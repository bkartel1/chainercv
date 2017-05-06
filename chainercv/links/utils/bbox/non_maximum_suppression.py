from __future__ import division
import numpy as np

from chainer import cuda

from chainercv.links.utils.bbox._nms_gpu_post import _nms_gpu_post


if cuda.available:
    import cupy

    @cupy.util.memoize(for_each_device=True)
    def _load_kernel(kernel_name, code, options=()):
        assert isinstance(options, tuple)
        kernel_code = cupy.cuda.compile_with_cache(code, options=options)
        return kernel_code.get_function(kernel_name)


def non_maximum_suppression(bbox, thresh, score=None,
                            limit=None):
    """Suppress bounding boxes according to their Jaccard overlap.

    This method checks each bounding box sequentially and selects the bounding
    box if the Jaccard overlap between the bounding box and the previously
    selected bounding boxes is less than :obj:`thresh`. This method is
    mainly used as postprocessing of object detection.
    The bounding boxes are selected from ones with higher scores.
    If :obj:`score` is not provided as an argument, the bounding box
    is ordered by its index in ascending order.

    The bounding boxes are expected to be packed into a two dimensional
    tensor of shape :math:`(R, 4)`, where :math:`R` is the number of
    bounding boxes in the image. The second axis represents attributes of
    the bounding box. They are :obj:`(x_min, y_min, x_max, y_max)`,
    where the four attributes are coordinates of the bottom left and the
    top right vertices.

    :obj:`score` is a float array of shape :math:`(R,)`. Each score indicates
    confidence of prediction.

    This function accepts both :obj:`numpy.ndarray` and :obj:`cupy.ndarray` as
    inputs. Please note that both :obj:`bbox` and :obj:`score` need to be
    same type.
    The output is same type as the type of the inputs.

    Args:
        bbox (array): Bounding boxes to be transformed. The shape is
            :math:`(R, 4)`. :math:`R` is the number of bounding boxes.
        thresh (float): Thresold of Jaccard overlap.
        score (array): An array of confidences whose shape is :math:`(R,)`.
        limit (int): The upper bound of the number of the output bounding
            boxes. If it is not specified, this method selects as many
            bounding boxes as possible.

    Returns:
        array:
        An array with indices of bounding boxes that are selected. \
        The shape of this array is :math:`(R,)` and its dtype is\
        :obj:`numpy.int32`.

    """

    xp = cuda.get_array_module(bbox)
    if xp == np:
        return _non_maximum_suppression_cpu(bbox, thresh, score, limit)
    else:
        return _non_maximum_suppression_gpu(bbox, thresh, score, limit)


def _non_maximum_suppression_cpu(bbox, thresh, score=None, limit=None):
    if len(bbox) == 0:
        return np.zeros((0,), dtype=np.int32)

    bbox_area = np.prod(bbox[:, 2:] - bbox[:, :2], axis=1)
    if score is not None:
        order = score.argsort()[::-1]
        bbox = bbox[order]

    selec = np.zeros(bbox.shape[0], dtype=bool)
    for i, b in enumerate(bbox):
        lt = np.maximum(b[:2], bbox[selec, :2])
        rb = np.minimum(b[2:], bbox[selec, 2:])
        area = np.prod(rb - lt, axis=1) * (lt < rb).all(axis=1)

        jaccard = area / (bbox_area[i] + bbox_area[selec] - area)
        if (jaccard >= thresh).any():
            continue

        selec[i] = True
        if limit is not None and np.count_nonzero(selec) >= limit:
            break

    selec = np.where(selec)[0]
    if score is not None:
        selec = order[selec]
    return selec.astype(np.int32)


def _non_maximum_suppression_gpu(bbox, thresh, score=None, limit=None):
    if len(bbox) == 0:
        return cupy.zeros((0,), dtype=np.int32)

    n_bbox = bbox.shape[0]

    if score is not None:
        # CuPy does not currently support argsort.
        order = cuda.to_cpu(score).argsort()[::-1].astype(np.int32)
        order = cuda.to_gpu(order)
    else:
        order = cupy.arange(n_bbox, dtype=np.int32)

    sorted_bbox = bbox[order, :]
    selec, n_selec = _call_nms_kernel(
        sorted_bbox, n_bbox, thresh)
    selec = selec[:n_selec]
    selec = order[selec]
    if limit is not None:
        selec = selec[:limit]
    return selec


nms_gpu_code = '''
#include <vector>

#define DIVUP(m,n) ((m) / (n) + ((m) % (n) > 0))
int const threadsPerBlock = sizeof(unsigned long long) * 8;

__device__ inline float devIoU(float const * const a, float const * const b) {
  float left = max(a[0], b[0]), right = min(a[2], b[2]);
  float top = max(a[1], b[1]), bottom = min(a[3], b[3]);
  float width = max(right - left, 0.f), height = max(bottom - top, 0.f);
  float interS = width * height;
  float Sa = (a[2] - a[0]) * (a[3] - a[1]);
  float Sb = (b[2] - b[0]) * (b[3] - b[1]);
  return interS / (Sa + Sb - interS);
}

extern "C"
__global__
void nms_kernel(const int n_bbox, const float thresh,
                           const float *dev_bbox,
                           unsigned long long *dev_mask) {
  const int row_start = blockIdx.y;
  const int col_start = blockIdx.x;

  // if (row_start > col_start) return;

  const int row_size =
        min(n_bbox - row_start * threadsPerBlock, threadsPerBlock);
  const int col_size =
        min(n_bbox - col_start * threadsPerBlock, threadsPerBlock);

  __shared__ float block_boxes[threadsPerBlock * 4];
  if (threadIdx.x < col_size) {
    block_boxes[threadIdx.x * 4 + 0] =
        dev_bbox[(threadsPerBlock * col_start + threadIdx.x) * 4 + 0];
    block_boxes[threadIdx.x * 4 + 1] =
        dev_bbox[(threadsPerBlock * col_start + threadIdx.x) * 4 + 1];
    block_boxes[threadIdx.x * 4 + 2] =
        dev_bbox[(threadsPerBlock * col_start + threadIdx.x) * 4 + 2];
    block_boxes[threadIdx.x * 4 + 3] =
        dev_bbox[(threadsPerBlock * col_start + threadIdx.x) * 4 + 3];
  }
  __syncthreads();

  if (threadIdx.x < row_size) {
    const int cur_box_idx = threadsPerBlock * row_start + threadIdx.x;
    const float *cur_box = dev_bbox + cur_box_idx * 4;
    int i = 0;
    unsigned long long t = 0;
    int start = 0;
    if (row_start == col_start) {
      start = threadIdx.x + 1;
    }
    for (i = start; i < col_size; i++) {
      if (devIoU(cur_box, block_boxes + i * 4) >= thresh) {
        t |= 1ULL << i;
      }
    }
    const int col_blocks = DIVUP(n_bbox, threadsPerBlock);
    dev_mask[cur_box_idx * col_blocks + col_start] = t;
  }
}
'''


def _call_nms_kernel(bbox, n_bbox, thresh):
    threads_per_block = 64
    col_blocks = np.ceil(n_bbox / threads_per_block).astype(np.int32)
    blocks = (col_blocks, col_blocks, 1)
    threads = (threads_per_block, 1, 1)

    mask_dev = cupy.zeros((n_bbox * col_blocks,), dtype=np.uint64)
    bbox = cupy.ascontiguousarray(bbox, dtype=np.float32)
    kern = _load_kernel('nms_kernel', nms_gpu_code)
    kern(blocks, threads, args=(n_bbox, cupy.float32(thresh),
                                bbox, mask_dev))

    mask_host = mask_dev.get()
    selection, n_selec = _nms_gpu_post(
        mask_host, n_bbox, threads_per_block, col_blocks)
    return selection, n_selec
