import re
import logging
import os.path
import threading

from queue import Queue
from functools import lru_cache

import psutil
import numpy as np
import skimage.external.tifffile as tiff

from .fuse import fuse_queue
from .overlaps import Overlaps
from .lcd_numbers import numbers, canvas_shape

from ..inputfile import InputFile

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def to_dtype(x, dtype):
    if np.issubdtype(dtype, np.integer):
        np.rint(x, x)
    return x.astype(dtype, copy=False)


class FuseRunner(object):
    def __init__(self, file_matrix):
        self.fm = file_matrix  #: :class:`FileMatrix`
        self.path, _ = os.path.split(self.fm.input_path)

        self.zmin = 0
        self.zmax = None
        self.debug = False
        self.output_filename = None

        self._is_multichannel = None

    @property
    @lru_cache()
    def dtype(self):
        infile = os.path.join(self.path, self.fm.data_frame.iloc[0].name)
        with InputFile(infile) as f:
            return np.dtype(f.dtype)

    @property
    @lru_cache()
    def is_multichannel(self):
        infile = os.path.join(self.path, self.fm.data_frame.iloc[0].name)
        with InputFile(infile) as f:
            if f.nchannels > 1:
                multichannel = True
            else:
                multichannel = False
        return multichannel

    @property
    def output_shape(self):
        thickness = self.fm.full_thickness
        if self.zmax is not None:
            thickness -= (thickness - self.zmax)
        thickness -= self.zmin

        infile = os.path.join(self.path, self.fm.data_frame.iloc[0].name)
        with InputFile(infile) as f:
            output_shape = list(f.shape)

        output_shape[0] = thickness
        output_shape[-2] = self.fm.full_height
        output_shape[-1] = self.fm.full_width

        return output_shape

    def run(self):
        ov = Overlaps(self.fm)

        total_byte_size = np.asscalar(np.prod(self.output_shape)
                                      * self.dtype.itemsize)
        bigtiff = total_byte_size > 2**31 - 1

        ram = psutil.virtual_memory().total

        # size in bytes of an xy plane (including channels) (float32)
        xy_size = np.asscalar(np.prod(self.output_shape[1::]) * 4)
        n_frames_in_ram = int(ram / xy_size / 1.5)

        n_loops = self.output_shape[0] // n_frames_in_ram

        partial_thickness = [n_frames_in_ram for _ in range(0, n_loops)]
        remainder = self.output_shape[0] % n_frames_in_ram
        if remainder:
            partial_thickness += [remainder]

        try:
            os.remove(self.output_filename)
        except FileNotFoundError:
            pass

        for thickness in partial_thickness:
            self.zmax = self.zmin + thickness
            fused = np.zeros(self.output_shape, dtype=np.float32)
            q = Queue(maxsize=20)

            t = threading.Thread(target=fuse_queue,
                                 args=(q, fused, self.debug))
            t.start()

            for index, row in self.fm.data_frame.iterrows():
                if self.zmax is None:
                    z_to = row.nfrms
                else:
                    z_to = self.zmax - row.Zs

                if z_to > row.nfrms:
                    z_to = row.nfrms

                if z_to <= 0:
                    continue

                z_from = self.zmin - row.Zs

                if z_from < 0:
                    z_from = 0

                if z_from >= z_to:
                    continue

                with InputFile(os.path.join(self.path, index)) as f:
                    logger.info(
                        'opening {}\tz=[{}:{}]'.format(index, z_from, z_to))
                    slice = f.slice(z_from, z_to, dtype=np.float32, copy=True)

                if self.debug:
                    self.overlay_debug(slice, index, z_from)

                top_left = [row.Zs + z_from - self.zmin, row.Ys, row.Xs]
                overlaps = ov.overlaps(index).copy()
                overlaps = overlaps.loc[
                    (overlaps['Z_from'] <= z_to) & (overlaps['Z_to'] >= z_from)
                ]

                overlaps['Z_from'] -= z_from
                overlaps['Z_to'] -= z_from

                overlaps.loc[overlaps['Z_from'] < 0, 'Z_from'] = 0

                q.put([slice, top_left, overlaps])

            q.put([None, None, None])  # close queue

            t.join()  # wait for fuse thread to finish
            print('=================================')

            if self.is_multichannel:
                fused = np.moveaxis(fused, -3, -1)

            fused = to_dtype(fused, self.dtype)
            logger.info('saving output to {}'.format(self.output_filename))
            tiff.imsave(self.output_filename, fused, append=True,
                        bigtiff=bigtiff)

            self.zmin += thickness

    def overlay_debug(self, slice, index, z_from):
        cx = slice.shape[-1] // 2
        cy = slice.shape[-2] // 2 + 10
        x = cx - cx // 2
        for xstr in re.findall(r'\d+', index):
            for l in xstr:
                x_end = x + canvas_shape[1]
                try:
                    slice[..., cy:cy + canvas_shape[0], x:x_end] = \
                        numbers[int(l)]
                except ValueError:
                    break
                x = x_end + canvas_shape[1] // 2
            x = x_end + canvas_shape[1]

        cy += int(canvas_shape[0] * 1.4)
        for f in range(0, slice.shape[0]):
            x = cx
            xstr = str(z_from + f)
            for l in xstr:
                x_end = x + canvas_shape[1]
                try:
                    slice[f, ..., cy:cy + canvas_shape[0], x:x_end] = \
                        numbers[int(l)]
                except ValueError:
                    break
                x = x_end + canvas_shape[1] // 2