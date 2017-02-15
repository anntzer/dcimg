# Based on:
# https://github.com/StuartLittlefair/dcimg/blob/master/dcimg/Raw.py
# hamamatsuOrcaTools: https://github.com/orlandi/hamamatsuOrcaTools
# Python Microscopy: http://www.python-microscopy.org
#                    https://bitbucket.org/david_baddeley/python-microscopy

import mmap
import numpy as np
from math import pow, log10, floor


class DCIMGFile(object):
    """A DCIMG file (Hamamatsu format), memory-mapped.

    After use, call the close() method to release resources properly.
    """

    FILE_HDR_DTYPE = [
        ('file_format', 'S8'),
        ('format_version', '<u4'),  # 0x08
        ('skip', '5<u4'),           # 0x0c
        ('nsess', '<u4'),           # 0x20 ?
        ('nfrms', '<u4'),           # 0x24
        ('header_size', '<u4'),     # 0x28 ?
        ('skip2', '<u4'),           # 0x2c
        ('file_size', '<u8'),       # 0x30
        ('skip3', '2<u4'),          # 0x38
        ('file_size2', '<u8'),      # 0x40, repeated
    ]

    SESS_HDR_DTYPE = [
        ('session_size', '<u8'),  # including footer
        ('skip1', '6<u4'),
        ('nfrms', '<u4'),
        ('byte_depth', '<u4'),
        ('skip2', '<u4'),
        ('xsize', '<u4'),
        ('bytes_per_row', '<u4'),
        ('ysize', '<u4'),
        ('bytes_per_img', '<u4'),
        ('skip3', '2<u4'),
        ('header_size', '1<u4'),
        ('session_data_size', '<u8'),  # header_size + x*y*byte_depth*nfrms
    ]

    def __init__(self, file_name=None):
        self.mm = None  #: memory-mapped array
        self.file_header = None
        self.sess_header = None
        self.file_size = None
        self.dtype = None
        self.file_name = file_name
        if file_name is not None:
            self.open()

    def open(self, file_name=None):
        self.close()
        if file_name is None:
            file_name = self.file_name

        with open(file_name, 'r') as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_COPY)
            if mm[:5] != b"DCIMG":
                mm.close()

        self.mm = mm
        self._parse_header()

    @property
    def nfrms(self):
        return self.sess_header['nfrms'][0]

    @property
    def byte_depth(self):
        """Number of bytes per pixel."""
        return self.sess_header['byte_depth'][0]

    @property
    def xsize(self):
        return self.sess_header['xsize'][0]

    @property
    def ysize(self):
        return self.sess_header['ysize'][0]

    @property
    def bytes_per_row(self):
        return self.sess_header['bytes_per_row'][0]

    @property
    def bytes_per_img(self):
        return self.sess_header['bytes_per_img'][0]

    @property
    def shape(self):
        return (self.nfrms, self.xsize, self.ysize)

    @property
    def header_size(self):
        return self.file_header['header_size'][0]

    @property
    def session_footer_offset(self):
        return int(self.header_size + self.sess_header['session_data_size'][0])

    @property
    def timestamp_offset(self):
        return int(self.session_footer_offset + 272 + 4 * self.nfrms)

    def close(self):
        if self.mm is not None:
            self.mm.close()
        self.mm = None

    def _parse_header(self):
        self.file_header = np.zeros(1, dtype=self.FILE_HDR_DTYPE)
        self.file_header = np.fromstring(self.mm[0:self.file_header.nbytes],
                                         dtype=self.FILE_HDR_DTYPE)

        if not self.file_header['file_format'] == b'DCIMG':
            raise RuntimeError('Invalid DCIMG file')

        self.sess_header = np.zeros(1, dtype=self.SESS_HDR_DTYPE)
        index_from = self.header_size
        index_to = index_from + self.sess_header.nbytes
        self.sess_header = np.fromstring(self.mm[index_from:index_to],
                                         dtype=self.SESS_HDR_DTYPE)

        if self.byte_depth == 1:
            self.dtype = np.uint8
        elif self.byte_depth == 2:
            self.dtype = np.uint16
        else:
            raise RuntimeError(
                "Invalid byte-depth: {}".format(self.byte_depth))

        if self.bytes_per_row != self.byte_depth * self.ysize:
            e_str = "bytes_per_row ({bytes_per_row}) " \
                    "!= byte_depth ({byte_depth}) * nrows ({y_size})" \
                .format(**vars(self))
            raise RuntimeError(e_str)

        if self.bytes_per_img != self.bytes_per_row * self.ysize:
            e_str = "bytes per img ({bytes_per_img}) != nrows ({y_size}) * " \
                    "bytes_per_row ({bytes_per_row})".format(**vars(self))
            raise RuntimeError(e_str)

    @property
    def timestamps(self):
        """A numpy array with frame timestamps."""
        ts = np.zeros(self.nfrms)
        index = self.timestamp_offset
        for i in range(0, self.nfrms):
            whole = int.from_bytes(self.mm[index:index + 4], 'little')
            index += 4

            fraction = int.from_bytes(self.mm[index:index + 4], 'little')
            index += 4

            val = whole
            if fraction != 0:
                val += fraction * pow(10, -(floor(log10(fraction)) + 1))
            ts[i] = val

        return ts

    def layer(self, index, frames_per_layer=1, dtype=None):
        """Return a layer, i.e. a stack of frames.

        Parameters
        ----------
        index : layer index
        frames_per_layer : number of frames per layer
        dtype

        Returns
        -------
        A numpy array of the original type or of dtype, if specified. The
        shape of the array is (nframes, ysize, xsize).
        """
        offset = 232 + self.bytes_per_img * frames_per_layer * index
        a = np.ndarray((frames_per_layer, self.ysize, self.xsize),
                       self.dtype, self.mm, offset)

        # retrieve the first 4 pixels of each frame, which are stored in the
        # file footer. Will overwrite [0000, FFFF, 0000, FFFF, 0000] at the
        # beginning of the frame.
        index = (self.session_footer_offset + 272
                 + self.nfrms * (4 + 8)  # 4 for frame count, 8 for timestamp
                 + 4 * self.byte_depth * index * frames_per_layer)
        for i in range(0, frames_per_layer):
            px = np.ndarray((1, 1, 4), self.dtype, self.mm, index)
            a[i, 0, 0:4] = px
            index += 4 * self.byte_depth

        if dtype is None:
            return a
        return a.astype(dtype)

    def frame(self, index, dtype=None):
        """Convenience function to retrieve a single layer.

        Same as calling layer() with frames_per_layer=1.

        Parameters
        ----------
        index : layer index
        dtype

        Returns
        -------
        A numpy array of the original type or of dtype, if specified. The
        shape of the array is (ysize, xsize).
        """
        return np.squeeze(self.layer(index), dtype)
