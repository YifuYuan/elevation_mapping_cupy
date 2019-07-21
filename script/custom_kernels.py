import cupy as cp
import string


def map_utils(resolution, width, height, sensor_noise_factor):
    util_preamble = string.Template('''
        __device__ float16 clamp(float16 x, float16 min_x, float16 max_x) {

            return max(min(x, max_x), min_x);
        }
        __device__ float16 round(float16 x) {
            return (int)x + (int)(2 * (x - (int)x));
        }
        __device__ int get_xy_idx(float16 x, float16 center) {
            const float resolution = ${resolution};
            int i = round((x - center) / resolution);
            return i;
        }
        __device__ int get_idx(float16 x, float16 y, float16 center_x, float16 center_y) {
            int idx_x = clamp(get_xy_idx(x, center_x) + ${width} / 2, 0, ${width} - 1);
            int idx_y = clamp(get_xy_idx(y, center_y) + ${height} / 2, 0, ${height} - 1);
            return ${width} * idx_x + idx_y;
        }
        __device__ int get_map_idx(int idx, int layer_n) {
            const int layer = ${width} * ${height};
            return layer * layer_n + idx;
        }
        __device__ float transform_p(float16 x, float16 y, float16 z,
                                     float16 r0, float16 r1, float16 r2, float16 t) {
            return r0 * x + r1 * y + r2 * z + t;
        }
        __device__ float z_noise(float16 z){
            return ${sensor_noise_factor} * z * z;
        }

        ''').substitute(resolution=resolution, width=width, height=height,
                        sensor_noise_factor=sensor_noise_factor)
    return util_preamble


def add_points_kernel(resolution, width, height, sensor_noise_factor,
                      mahalanobis_thresh, outlier_variance, wall_num_thresh,
                      enable_edge_shaped=True):

    add_points_kernel = cp.ElementwiseKernel(
            in_params='raw U p, U center_x, U center_y, raw U R, raw U t',
            out_params='raw U map, raw T newmap',
            preamble=map_utils(resolution, width, height, sensor_noise_factor),
            operation=\
            string.Template(
            '''
            U rx = p[i * 3];
            U ry = p[i * 3 + 1];
            U rz = p[i * 3 + 2];
            U x = transform_p(rx, ry, rz, R[0], R[1], R[2], t[0]);
            U y = transform_p(rx, ry, rz, R[3], R[4], R[5], t[1]);
            U z = transform_p(rx, ry, rz, R[6], R[7], R[8], t[2]);
            U v = z_noise(rz);
            int idx = get_idx(x, y, center_x, center_y);
            U map_h = map[get_map_idx(idx, 0)];
            U map_v = map[get_map_idx(idx, 1)];
            U num_points = newmap[get_map_idx(idx, 3)];
            if (abs(map_h - z) > (map_v * ${mahalanobis_thresh})) {
                atomicAdd(&map[get_map_idx(idx, 1)], ${outlier_variance});
            }
            else {
                if (${enable_edge_shaped} && num_points > ${wall_num_thresh} && z < map_h) { continue; }
                T new_h = (map_h * v + z * map_v) / (map_v + v);
                T new_v = (map_v * v) / (map_v + v);
                atomicAdd(&newmap[get_map_idx(idx, 0)], new_h);
                atomicAdd(&newmap[get_map_idx(idx, 1)], new_v);
                atomicAdd(&newmap[get_map_idx(idx, 2)], 1.0);
                map[get_map_idx(idx, 2)] = 1;
            }
            ''').substitute(mahalanobis_thresh=mahalanobis_thresh,
                            outlier_variance=outlier_variance,
                            wall_num_thresh=wall_num_thresh,
                            enable_edge_shaped=int(enable_edge_shaped)),
            name='add_points_kernel')
    return add_points_kernel


def error_counting_kernel(resolution, width, height, sensor_noise_factor,
                          mahalanobis_thresh, outlier_variance, traversability_inlier):

    error_counting_kernel = cp.ElementwiseKernel(
            in_params='raw U map, raw U p, U center_x, U center_y, raw U R, raw U t',
            out_params='raw U newmap, raw T error, raw T error_cnt',
            preamble=map_utils(resolution, width, height, sensor_noise_factor),
            operation=\
            string.Template(
            '''
            U rx = p[i * 3];
            U ry = p[i * 3 + 1];
            U rz = p[i * 3 + 2];
            U x = transform_p(rx, ry, rz, R[0], R[1], R[2], t[0]);
            U y = transform_p(rx, ry, rz, R[3], R[4], R[5], t[1]);
            U z = transform_p(rx, ry, rz, R[6], R[7], R[8], t[2]);
            U v = z_noise(rz);
            int idx = get_idx(x, y, center_x, center_y);
            U map_h = map[get_map_idx(idx, 0)];
            U map_v = map[get_map_idx(idx, 1)];
            U map_t = map[get_map_idx(idx, 3)];
            if (abs(map_h - z) < (map_v * ${mahalanobis_thresh})
                && map_v < ${outlier_variance} / 2.0
                && map_t < ${traversability_inlier}) {
                T e = z - map_h;
                atomicAdd(&error[0], e);
                atomicAdd(&error_cnt[0], 1);
                atomicAdd(&newmap[get_map_idx(idx, 3)], 1.0);
            }
            ''').substitute(mahalanobis_thresh=mahalanobis_thresh,
                            outlier_variance=outlier_variance,
                            traversability_inlier=traversability_inlier),
            name='error_counting_kernel')
    return error_counting_kernel


def average_map_kernel(width, height, max_variance, initial_variance):
    average_map_kernel = cp.ElementwiseKernel(
            in_params='raw U newmap',
            out_params='raw U map',
            preamble=\
            string.Template('''
            __device__ int get_map_idx(int idx, int layer_n) {
                const int layer = ${width} * ${height};
                return layer * layer_n + idx;
            }
            ''').substitute(width=width, height=height),
            operation=\
            string.Template('''
            U h = map[get_map_idx(i, 0)];
            U v = map[get_map_idx(i, 1)];
            U new_h = newmap[get_map_idx(i, 0)];
            U new_v = newmap[get_map_idx(i, 1)];
            U new_cnt = newmap[get_map_idx(i, 2)];
            if (new_cnt > 0) {
                if (new_v / new_cnt > ${max_variance}) {
                    map[get_map_idx(i, 0)] = 0;
                    map[get_map_idx(i, 1)] = ${initial_variance};
                    map[get_map_idx(i, 2)] = 0;
                }
                else {
                    map[get_map_idx(i, 0)] = new_h / new_cnt;
                    map[get_map_idx(i, 1)] = new_v / new_cnt;
                    map[get_map_idx(i, 2)] = 1;
                }
            }
            ''').substitute(max_variance=max_variance,
                            initial_variance=initial_variance),
            name='average_map_kernel')
    return average_map_kernel


def dilation_filter_kernel(width, height, dilation_size):
    dilation_filter_kernel = cp.ElementwiseKernel(
            in_params='raw U map, raw U mask',
            out_params='raw U newmap',
            preamble=\
            string.Template('''
            __device__ int get_map_idx(int idx, int layer_n) {
                const int layer = ${width} * ${height};
                return layer * layer_n + idx;
            }

            __device__ int get_relative_map_idx(int idx, int dx, int dy, int layer_n) {
                const int layer = ${width} * ${height};
                const int relative_idx = idx + ${width} * dy + dx;
                return layer * layer_n + relative_idx;
            }
            ''').substitute(width=width, height=height),
            operation=\
            string.Template('''
            U h = map[get_map_idx(i, 0)];
            U valid = mask[get_map_idx(i, 0)];
            newmap[get_map_idx(i, 0)] = h;
            if (valid < 0.5) {
                U distance = 100;
                U near_value = 0;
                for (int dy = -${dilation_size}; dy <= ${dilation_size}; dy++) {
                    for (int dx = -${dilation_size}; dx <= ${dilation_size}; dx++) {
                        U valid = mask[get_relative_map_idx(i, dx, dy, 0)];
                        if(valid > 0.5 && dx + dy < distance) {
                            distance = dx + dy;
                            near_value = map[get_relative_map_idx(i, dx, dy, 0)];
                        }
                    }
                }
                if(distance < 100) {
                    newmap[get_map_idx(i, 0)] = near_value;
                    // newmap[get_map_idx(i, 0)] = 10;
                }
            }
            ''').substitute(dilation_size=dilation_size),
            name='dilation_filter_kernel')
    return dilation_filter_kernel


# def polygon_mask_kernel(width, height, resolution):
#     polygon_mask_kernel = cp.ElementwiseKernel(
#             in_params='raw U polygon, U center_x, U center_y, int16 polygon_n, raw U polygon_bbox',
#             out_params='raw U mask',
#             preamble=\
#             string.Template('''
#             __device__ int get_map_idx(int idx, int layer_n) {
#                 const int layer = ${width} * ${height};
#                 return layer * layer_n + idx;
#             }
#
#             __device__ float16 get_idx_x(int idx, float16 center_x) {
#                 int idx_x = idx / ${width};
#                 float16 x = (idx_x - ${width} / 2) * ${resolution} + center_x;
#                 return x;
#             }
#             __device__ float16 get_idx_y(int idx, float16 center_y) {
#                 int idx_y = idx % ${width};
#                 float16 y = (idx_y - ${height} / 2) * ${resolution} + center_y;
#                 return y;
#             }
#
#             __device__ bool intersect(float16 x1, float16 y1,
#                                       float16 x2, float16 y2,
#                                       float16 x3, float16 y3,
#                                       float16 x4, float16 y4) {
#                 float16 tc1 = (x1 - x2) * (y3 - y1) + (y1 - y2) * (x1 - x3);
#                 float16 tc2 = (x1 - x2) * (y4 - y1) + (y1 - y2) * (x1 - x4);
#                 float16 td1 = (x3 - x4) * (y1 - y3) + (y3 - y4) * (x3 - x1);
#                 float16 td2 = (x3 - x4) * (y2 - y1) + (y3 - y4) * (x3 - x2);
#                 if ((tc1 * tc2 < 0) && (td1 * td2 < 0)) {
#                     return true;
#                 }
#                 else {
#                     return false;
#                 }
#             }
#             ''').substitute(width=width, height=height, resolution=resolution),
#             operation=\
#             string.Template('''
#             float16 x = get_idx_x(i, center_x);
#             float16 y = get_idx_y(i, center_y);
#             if (x < polygon_bbox[0] || x > polygon_bbox[2]
#                 || y < polygon_bbox[1] || y > polygon_bbox[3]){
#                 mask[i] = 0;
#             }
#             else {
#                 int intersect_cnt = 0;
#                 for (int j = 0; j < polygon_n; j++) {
#                     float16 p1x = polygon[j * 2 + 0];
#                     float16 p1y = polygon[j * 2 + 1];
#                     float16 p2x = polygon[(j + 1) % polygon_n * 2 + 0];
#                     float16 p2y = polygon[(j + 1) % polygon_n * 2 + 1];
#                     if(intersect(x, 0, x, y, p1x, p1y, p2x, p2y)) {
#                         if(((p1y <= y) && (p2y > y))
#                                 || ((p1y > y) && (p2y <= y))){
#                             intersect_cnt++;
#                         }
#                     }
#                 }
#                 if (intersect_cnt % 2 == 0) { mask[i] = 0; }
#                 else { mask[i] = 1; }
#             }
#             ''').substitute(a=1),
#             name='polygon_mask_kernel')
#     return polygon_mask_kernel


def polygon_mask_kernel(width, height, resolution):
    polygon_mask_kernel = cp.ElementwiseKernel(
            in_params='raw U polygon, U center_x, U center_y, int16 polygon_n, raw U polygon_bbox',
            out_params='raw U mask',
            preamble=\
            string.Template('''
            __device__ struct Point
            {
                int x;
                int y;
            };
            // Given three colinear points p, q, r, the function checks if
            // point q lies on line segment 'pr'
            __device__ bool onSegment(Point p, Point q, Point r)
            {
                if (q.x <= max(p.x, r.x) && q.x >= min(p.x, r.x) &&
                        q.y <= max(p.y, r.y) && q.y >= min(p.y, r.y))
                    return true;
                return false;
            }
            // To find orientation of ordered triplet (p, q, r).
            // The function returns following values
            // 0 --> p, q and r are colinear
            // 1 --> Clockwise
            // 2 --> Counterclockwise
            __device__ int orientation(Point p, Point q, Point r)
            {
                int val = (q.y - p.y) * (r.x - q.x) -
                          (q.x - p.x) * (r.y - q.y);
                if (val == 0) return 0;  // colinear
                return (val > 0)? 1: 2; // clock or counterclock wise
            }
            // The function that returns true if line segment 'p1q1'
            // and 'p2q2' intersect.
            __device__ bool doIntersect(Point p1, Point q1, Point p2, Point q2)
            {
                // Find the four orientations needed for general and
                // special cases
                int o1 = orientation(p1, q1, p2);
                int o2 = orientation(p1, q1, q2);
                int o3 = orientation(p2, q2, p1);
                int o4 = orientation(p2, q2, q1);
                // General case
                if (o1 != o2 && o3 != o4)
                    return true;
                // Special Cases
                // p1, q1 and p2 are colinear and p2 lies on segment p1q1
                if (o1 == 0 && onSegment(p1, p2, q1)) return true;
                // p1, q1 and p2 are colinear and q2 lies on segment p1q1
                if (o2 == 0 && onSegment(p1, q2, q1)) return true;
                // p2, q2 and p1 are colinear and p1 lies on segment p2q2
                if (o3 == 0 && onSegment(p2, p1, q2)) return true;
                 // p2, q2 and q1 are colinear and q1 lies on segment p2q2
                if (o4 == 0 && onSegment(p2, q1, q2)) return true;
                return false; // Doesn't fall in any of the above cases
            }
            __device__ int get_map_idx(int idx, int layer_n) {
                const int layer = ${width} * ${height};
                return layer * layer_n + idx;
            }

            __device__ int get_idx_x(int idx){
                int idx_x = idx / ${width};
                return idx_x;
            }

            __device__ int get_idx_y(int idx){
                int idx_y = idx % ${width};
                return idx_y;
            }

            __device__ float16 clamp(float16 x, float16 min_x, float16 max_x) {

                return max(min(x, max_x), min_x);
            }
            __device__ float16 round(float16 x) {
                return (int)x + (int)(2 * (x - (int)x));
            }
            __device__ int get_xy_idx(float16 x, float16 center) {
                const float resolution = ${resolution};
                int i = round((x - center) / resolution);
                return i;
            }
            __device__ int get_idx(float16 x, float16 y, float16 center_x, float16 center_y) {
                int idx_x = clamp(get_xy_idx(x, center_x) + ${width} / 2, 0, ${width} - 1);
                int idx_y = clamp(get_xy_idx(y, center_y) + ${height} / 2, 0, ${height} - 1);
                return ${width} * idx_x + idx_y;
            }

            __device__ bool intersect(float16 x1, float16 y1,
                                      float16 x2, float16 y2,
                                      float16 x3, float16 y3,
                                      float16 x4, float16 y4) {
                float16 tc1 = (x1 - x2) * (y3 - y1) + (y1 - y2) * (x1 - x3);
                float16 tc2 = (x1 - x2) * (y4 - y1) + (y1 - y2) * (x1 - x4);
                float16 td1 = (x3 - x4) * (y1 - y3) + (y3 - y4) * (x3 - x1);
                float16 td2 = (x3 - x4) * (y2 - y1) + (y3 - y4) * (x3 - x2);
                if ((tc1 * tc2 < 0) && (td1 * td2 < 0)) {
                    return true;
                }
                else {
                    return false;
                }
            }
            ''').substitute(width=width, height=height, resolution=resolution),
            operation=\
            string.Template('''
            // Point p = {get_idx_x(i, center_x), get_idx_y(i, center_y)};
            Point p = {get_idx_x(i), get_idx_y(i)};
            Point extreme = {100000, p.y}; 
            int bbox_min_idx = get_idx(polygon_bbox[0], polygon_bbox[1], center_x, center_y);
            int bbox_max_idx = get_idx(polygon_bbox[2], polygon_bbox[3], center_x, center_y);
            Point bmin = {get_idx_x(bbox_min_idx), get_idx_y(bbox_min_idx)};
            Point bmax = {get_idx_x(bbox_max_idx), get_idx_y(bbox_max_idx)};
            if (p.x < bmin.x || p.x > bmax.x
                || p.y < bmin.y || p.y > bmax.y){
                mask[i] = 0;
                return;
            }
            else {
                int intersect_cnt = 0;
                for (int j = 0; j < polygon_n; j++) {
                    Point p1, p2;
                    int i1 = get_idx(polygon[j * 2 + 0], polygon[j * 2 + 1], center_x, center_y);
                    p1.x = get_idx_x(i1);
                    p1.y = get_idx_y(i1);
                    int j2 = (j + 1) % polygon_n;
                    int i2 = get_idx(polygon[j2 * 2 + 0], polygon[j2 * 2 + 1], center_x, center_y);
                    p2.x = get_idx_x(i2);
                    p2.y = get_idx_y(i2);
                    if (doIntersect(p1, p2, p, extreme))
                    {
                        // If the point 'p' is colinear with line segment 'i-next',
                        // then check if it lies on segment. If it lies, return true,
                        // otherwise false
                        if (orientation(p1, p, p2) == 0) {
                            if (onSegment(p1, p, p2)){
                                mask[i] = 1;
                                return;
                            }
                        }
                        else if(((p1.y <= p.y) && (p2.y > p.y))
                                || ((p1.y > p.y) && (p2.y <= p.y))){
                            intersect_cnt++;
                        }
                    }
                }
                if (intersect_cnt % 2 == 0) { mask[i] = 0; }
                else { mask[i] = 1; }
            }
            ''').substitute(a=1),
            name='polygon_mask_kernel')
    return polygon_mask_kernel


if __name__ == '__main__':
    for i in range(10):
        import random
        a = cp.zeros((100, 100)) 
        n = random.randint(3, 5)

        # polygon = cp.array([[-1, -1], [3, 4], [2, 4], [1, 3]], dtype=float)
        polygon = cp.array([[(random.random() - 0.5) * 10, (random.random() - 0.5) * 10] for i in range(n)], dtype=float)
        print(polygon)
        polygon_min = polygon.min(axis=0)
        polygon_max = polygon.max(axis=0)
        polygon_bbox = cp.concatenate([polygon_min, polygon_max]).flatten()
        polygon_n = polygon.shape[0]
        print(polygon_bbox)
        # polygon_bbox = cp.array([-5, -5, 5, 5], dtype=float)
        polygon_mask = polygon_mask_kernel(100, 100, 0.1)
        import time
        start = time.time()
        polygon_mask(polygon, 0.0, 0.0, polygon_n, polygon_bbox, a, size=(100*100))
        print(time.time() - start)
        import pylab as plt
        print(a)
        plt.imshow(cp.asnumpy(a))
        plt.show()

