# standard library imports
import io
import logging
import struct

# 3rd party library imports
import numpy as np
from uuid import UUID

# local imports
from glymur import Jp2k
from .lib import tiff as libtiff
from .jp2box import UUIDBox


tag_dtype = {
    1: {'format': 'B', 'nbytes': 1},
    2: {'format': 'B', 'nbytes': 1},
    3: {'format': 'H', 'nbytes': 2},
    4: {'format': 'I', 'nbytes': 4},
    5: {'format': 'II', 'nbytes': 8},
    7: {'format': 'B', 'nbytes': 1},
    9: {'format': 'i', 'nbytes': 4},
    10: {'format': 'ii', 'nbytes': 8},
    11: {'format': 'f', 'nbytes': 4},
    12: {'format': 'd', 'nbytes': 8},
    13: {'format': 'I', 'nbytes': 4},
    16: {'format': 'Q', 'nbytes': 8},
    17: {'format': 'q', 'nbytes': 8},
    18: {'format': 'Q', 'nbytes': 8}
}


class Tiff2Jp2k(object):
    """
    Attributes
    ----------
    tiff_filename : path or str
        Path to TIFF file.
    jp2_filename : path or str
        Path to JPEG 2000 file to be written.
    tilesize : tuple
        The dimensions of a tile in the JP2K file.
    """

    def __init__(
        self, tiff_filename, jp2_filename, tilesize=None,
        verbosity=logging.CRITICAL, **kwargs
    ):

        self.tiff_filename = tiff_filename
        if not self.tiff_filename.exists():
            raise FileNotFoundError(f'{tiff_filename} does not exist')

        self.jp2_filename = jp2_filename
        self.tilesize = tilesize

        self.kwargs = kwargs

        self.logger = logging.getLogger('tiff2jp2')
        self.logger.setLevel(verbosity)
        ch = logging.StreamHandler()
        ch.setLevel(verbosity)
        self.logger.addHandler(ch)

    def __enter__(self):
        self.tiff_fp = libtiff.open(self.tiff_filename)
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        libtiff.close(self.tiff_fp)

    def run(self):

        self.copy_image()
        self.copy_metadata()

    def copy_metadata(self):
        """
        Copy over the TIFF IFD.  Place it in a UUID box.  Append to the JPEG
        2000 file.
        """
        # Make it an exif UUID.
        uuid = UUID('b14bf8bd-083d-4b43-a5ae-8cd7d5a6ce03')
        uuid = UUID(bytes=b'JpgTiffExif->JP2')

        # create a bytesio object for the IFD
        b = io.BytesIO()

        with open(self.tiff_filename, 'rb') as tfp:

            endian = self._process_header(b, tfp)
            self._process_tags(b, tfp, endian)

        # now create the entire payload.  this is the exif identifier plus the
        # IFD.  construct them separately because the IFD offsets don't know
        # anything about the exif identifier.
        payload = b'EXIF\0\0' + b.getvalue()

        # the length of the box is the length of the payload plus 8 bytes
        # to store the length of the box and the box ID
        box_length = len(payload) + 8 

        uuid_box = UUIDBox(uuid, payload, box_length)
        with open(self.jp2_filename, mode='ab') as f:
            uuid_box.write(f)

    def _process_tags(self, b, tfp, endianness):

        # how many tags?
        buffer = tfp.read(2)
        num_tags, = struct.unpack(endianness + 'H', buffer)

        write_buffer = struct.pack('<H', num_tags)
        b.write(write_buffer)

        # Ok, so now we have the IFD main body, but following that we have
        # the tag payloads that cannot fit into 4 bytes.

        # the IFD main body in the TIFF.  As it might be big endian, we cannot
        # just process it as one big chunk.
        buffer = tfp.read(num_tags * 12)

        start_of_tags_position = b.tell()
        after_ifd_position = start_of_tags_position + len(buffer)

        for idx in range(num_tags):

            b.seek(start_of_tags_position + idx * 12)

            tag_data = buffer[idx * 12:(idx + 1) * 12]

            tag, dtype, nvalues = struct.unpack(
                endianness + 'HHI', tag_data[:8]
            )

            payload_length = tag_dtype[dtype]['nbytes'] * nvalues

            if payload_length > 4:
                # the payload does not fit into the tag entry, so get the
                # payload from the offset
                offset, = struct.unpack(endianness + 'I', tag_data[8:])
                current_position = tfp.tell()
                tfp.seek(offset)
                payload_buffer = tfp.read(payload_length)
                tfp.seek(current_position)

                payload_format = tag_dtype[dtype]['format'] * nvalues
                payload = struct.unpack(endianness + payload_format, payload_buffer)

                new_offset = after_ifd_position
                outbuffer = struct.pack(
                    '<HHII', tag,  dtype, nvalues, new_offset
                )
                b.write(outbuffer)

                # now write the payload at the outlying position and then come
                # back to the same position in the file stream
                cpos = b.tell()
                b.seek(new_offset)

                out_format = '<' + tag_dtype[dtype]['format'] * nvalues
                outbuffer = struct.pack(out_format, *payload)
                b.write(outbuffer)

                after_ifd_position = b.tell()
                b.seek(cpos)

            else:
                # the payload DOES fit into the tag entry, so write it back
                # into the tag entry in the UUID
                if tag_dtype[dtype]['nbytes'] * nvalues == 1:
                    payload_buffer = tag_data[8]
                elif tag_dtype[dtype]['nbytes'] * nvalues == 2:
                    payload_buffer = tag_data[8:10]
                else:
                    payload_buffer = tag_data[8:]

                payload_format = tag_dtype[dtype]['format'] * nvalues

                payload = struct.unpack(
                    endianness + payload_format, payload_buffer
                )

                outbuffer = struct.pack('<HHI', tag,  dtype, nvalues)
                b.write(outbuffer)

                # we may need to alter the output format
                if payload_format in ['H', 'B']:
                    # just write it as an integer
                    payload_format = 'I'

                outbuffer = struct.pack('<' + payload_format, *payload)
                b.write(outbuffer)
            
    def _process_header(self, b, tfp):

        buffer = tfp.read(8)
        data = struct.unpack('BB', buffer[:2])

        # big endian or little endian?
        if data[0] == 73 and data[1] == 73:
            # little endian
            endian = '<'
        elif data[0] == 77 and data[1] == 77:
            # big endian
            endian = '>'
        else:
            msg = (
                f"The byte order indication in the TIFF header "
                f"({read_buffer[0:2]}) is invalid.  It should be either "
                f"{bytes([73, 73])} or {bytes([77, 77])}."
            )
            raise RuntimeError(msg)

        # version number and offset to the first IFD
        _, offset = struct.unpack(endian + 'HI', buffer[2:8])
        tfp.seek(offset)

        # write this header, no matter what is in the first 8 bytes of the
        # TIFF
        data = struct.pack('<BBHI', 73, 73, 42, 8)
        b.write(data)

        return endian

    def copy_image(self):

        if libtiff.isTiled(self.tiff_fp):
            isTiled = True
        else:
            isTiled = False

        photometric = libtiff.getFieldDefaulted(self.tiff_fp, 'Photometric')
        imagewidth = libtiff.getFieldDefaulted(self.tiff_fp, 'ImageWidth')
        imageheight = libtiff.getFieldDefaulted(self.tiff_fp, 'ImageLength')
        spp = libtiff.getFieldDefaulted(self.tiff_fp, 'SamplesPerPixel')
        sf = libtiff.getFieldDefaulted(self.tiff_fp, 'SampleFormat')
        bps = libtiff.getFieldDefaulted(self.tiff_fp, 'BitsPerSample')

        if sf != libtiff.SampleFormat.UINT:
            sf_string = [
                key for key in dir(libtiff.SampleFormat)
                if getattr(libtiff.SampleFormat, key) == sf
            ][0]
            msg = (
                f"The TIFF SampleFormat is {sf_string}.  Only UINT is "
                "supported."
            )
            raise RuntimeError(msg)

        if bps not in [8, 16]:
            msg = (
                f"The TIFF BitsPerSample is {bps}.  Only 8 and 16 bits per "
                "sample are supported."
            )
            raise RuntimeError(msg)

        if bps == 8 and sf == libtiff.SampleFormat.UINT:
            dtype = np.uint8
        if bps == 16 and sf == libtiff.SampleFormat.UINT:
            dtype = np.uint16

        if libtiff.isTiled(self.tiff_fp):
            tw = libtiff.getFieldDefaulted(self.tiff_fp, 'TileWidth')
            th = libtiff.getFieldDefaulted(self.tiff_fp, 'TileLength')
        else:
            tw = imagewidth
            rps = libtiff.getFieldDefaulted(self.tiff_fp, 'RowsPerStrip')
            num_strips = libtiff.numberOfStrips(self.tiff_fp)

        if self.tilesize is not None:
            jth, jtw = self.tilesize

            num_jp2k_tile_rows = int(np.ceil(imagewidth / jtw))
            num_jp2k_tile_cols = int(np.ceil(imagewidth / jtw))

        # Using the RGBA interface is the only reasonable way to deal with
        # them.
        if photometric in [
            libtiff.Photometric.YCBCR, libtiff.Photometric.PALETTE
        ]:
            use_rgba_interface = True
        else:
            use_rgba_interface = False

        jp2 = Jp2k(
            self.jp2_filename,
            shape=(imageheight, imagewidth, spp),
            tilesize=self.tilesize,
            **self.kwargs
        )

        if self.tilesize is None and libtiff.RGBAImageOK(self.tiff_fp):

            # if no jp2k tiling was specified and if the image is ok to read
            # via the RGBA interface, then just do that.
            msg = (
                "Reading using the RGBA interface, writing as a single tile "
                "image."
            )
            self.logger.info(msg)

            image = libtiff.readRGBAImageOriented(self.tiff_fp)

            if spp < 4:
                image = image[:, :, :3]

            jp2[:] = image

        elif isTiled and self.tilesize is not None:

            num_tiff_tile_cols = int(np.ceil(imagewidth / tw))

            partial_jp2_tile_rows = (imageheight / jth) != (imageheight // jth)
            partial_jp2_tile_cols = (imagewidth / jtw) != (imagewidth // jtw)

            rgba_tile = np.zeros((th, tw, 4), dtype=np.uint8)

            self.logger.debug(f'image:  {imageheight} x {imagewidth}')
            self.logger.debug(f'jptile:  {jth} x {jtw}')
            self.logger.debug(f'ttile:  {th} x {tw}')
            for idx, tilewriter in enumerate(jp2.get_tilewriters()):

                # populate the jp2k tile with tiff tiles
                self.logger.info(f'Tile:  #{idx}')

                jp2k_tile = np.zeros((jth, jtw, spp), dtype=dtype)
                tiff_tile = np.zeros((th, tw, spp), dtype=dtype)

                jp2k_tile_row = int(np.ceil(idx // num_jp2k_tile_cols))
                jp2k_tile_col = int(np.ceil(idx % num_jp2k_tile_cols))

                # the coordinates of the upper left pixel of the jp2k tile
                julr, julc = jp2k_tile_row * jth, jp2k_tile_col * jtw

                # loop while the upper left corner of the current tiff file is
                # less than the lower left corner of the jp2k tile
                r = julr
                while (r // th) * th < min(julr + jth, imageheight):
                    c = julc

                    tilenum = libtiff.computeTile(self.tiff_fp, c, r, 0, 0)

                    tiff_tile_row = int(np.ceil(tilenum // num_tiff_tile_cols))
                    tiff_tile_col = int(np.ceil(tilenum % num_tiff_tile_cols))

                    # the coordinates of the upper left pixel of the TIFF tile
                    tulr = tiff_tile_row * th
                    tulc = tiff_tile_col * tw

                    # loop while the left corner of the current tiff tile is
                    # less than the right hand corner of the jp2k tile
                    while ((c // tw) * tw) < min(julc + jtw, imagewidth):

                        if use_rgba_interface:
                            libtiff.readRGBATile(
                                self.tiff_fp, tulc, tulr, rgba_tile
                            )

                            # flip the tile upside down!!
                            tiff_tile = np.flipud(rgba_tile[:, :, :3])
                        else:
                            libtiff.readEncodedTile(
                                self.tiff_fp, tilenum, tiff_tile
                            )

                        # determine how to fit this tiff tile into the jp2k
                        # tile
                        #
                        # these are the section coordinates in image space
                        ulr = max(julr, tulr)
                        llr = min(julr + jth, tulr + th)

                        ulc = max(julc, tulc)
                        urc = min(julc + jtw, tulc + tw)

                        # convert to JP2K tile coordinates
                        jrows = slice(ulr % jth, (llr - 1) % jth + 1)
                        jcols = slice(ulc % jtw, (urc - 1) % jtw + 1)

                        # convert to TIFF tile coordinates
                        trows = slice(ulr % th, (llr - 1) % th + 1)
                        tcols = slice(ulc % tw, (urc - 1) % tw + 1)

                        jp2k_tile[jrows, jcols, :] = tiff_tile[trows, tcols, :]

                        # move exactly one tiff tile over
                        c += tw

                        tilenum = libtiff.computeTile(self.tiff_fp, c, r, 0, 0)

                        tiff_tile_row = int(
                            np.ceil(tilenum // num_tiff_tile_cols)
                        )
                        tiff_tile_col = int(
                            np.ceil(tilenum % num_tiff_tile_cols)
                        )

                        # the coordinates of the upper left pixel of the TIFF
                        # tile
                        tulr = tiff_tile_row * th
                        tulc = tiff_tile_col * tw

                    r += th

                # last tile column?  If so, we may have a partial tile.
                if (
                    partial_jp2_tile_cols
                    and jp2k_tile_col == num_jp2k_tile_cols - 1
                ):
                    last_j2k_cols = slice(0, jtw - (ulc - imagewidth))
                    jp2k_tile = jp2k_tile[:, jcols, :].copy()
                if (
                    partial_jp2_tile_rows
                    and jp2k_tile_row == num_jp2k_tile_rows - 1
                ):
                    last_j2k_rows = slice(0, jth - (llr - imageheight))
                    jp2k_tile = jp2k_tile[jrows, :, :].copy()

                tilewriter[:] = jp2k_tile

        elif not isTiled and self.tilesize is not None:

            num_strips = libtiff.numberOfStrips(self.tiff_fp)

            num_jp2k_tile_cols = int(np.ceil(imagewidth / jtw))

            partial_jp2_tile_rows = (imageheight / jth) != (imageheight // jth)
            partial_jp2_tile_cols = (imagewidth / jtw) != (imagewidth // jtw)

            tiff_strip = np.zeros((rps, imagewidth, spp), dtype=dtype)
            rgba_strip = np.zeros((rps, imagewidth, 4), dtype=np.uint8)

            for idx, tilewriter in enumerate(jp2.get_tilewriters()):
                self.logger.info(f'Tile: #{idx}')

                jp2k_tile = np.zeros((jth, jtw, spp), dtype=dtype)

                jp2k_tile_row = idx // num_jp2k_tile_cols
                jp2k_tile_col = idx % num_jp2k_tile_cols

                # the coordinates of the upper left pixel of the jp2k tile
                julr, julc = jp2k_tile_row * jth, jp2k_tile_col * jtw

                # Populate the jp2k tile with tiff strips.
                # Move by strips from the start of the jp2k tile to the bottom
                # of the jp2k tile.  That last strip may be partially empty,
                # worry about that later.
                #
                # loop while the upper left corner of the current tiff file is
                # less than the lower left corner of the jp2k tile
                r = julr
                while (r // rps) * rps < min(julr + jth, imageheight):

                    stripnum = libtiff.computeStrip(self.tiff_fp, r, 0)

                    if stripnum >= num_strips:
                        # we've moved past the end of the tiff
                        break

                    if use_rgba_interface:

                        # must use the first row in the strip
                        libtiff.readRGBAStrip(
                            self.tiff_fp, stripnum * rps, rgba_strip
                        )
                        # must flip the rows (!!) and get rid of the alpha
                        # plane
                        tiff_strip = np.flipud(rgba_strip[:, :, :spp])

                    else:
                        libtiff.readEncodedStrip(
                            self.tiff_fp, stripnum, tiff_strip
                        )

                    # the coordinates of the upper left pixel of the TIFF
                    # strip
                    tulr = stripnum * rps
                    tulc = 0

                    # determine how to fit this tiff strip into the jp2k
                    # tile
                    #
                    # these are the section coordinates in image space
                    ulr = max(julr, tulr)
                    llr = min(julr + jth, tulr + rps)

                    ulc = max(julc, tulc)
                    urc = min(julc + jtw, tulc + tw)

                    # convert to JP2K tile coordinates
                    jrows = slice(ulr % jth, (llr - 1) % jth + 1)
                    jcols = slice(ulc % jtw, (urc - 1) % jtw + 1)

                    # convert to TIFF strip coordinates
                    trows = slice(ulr % rps, (llr - 1) % rps + 1)
                    tcols = slice(ulc % tw, (urc - 1) % tw + 1)

                    jp2k_tile[jrows, jcols, :] = tiff_strip[trows, tcols, :]

                    r += rps

                # last tile column?  If so, we may have a partial tile.
                # j2k_cols is not sufficient here, must shorten it from 250
                # to 230
                if (
                    partial_jp2_tile_cols
                    and jp2k_tile_col == num_jp2k_tile_cols - 1
                ):
                    # decrease the number of columns by however many it sticks
                    # over the image width
                    last_j2k_cols = slice(0, jtw - (ulc + jtw - imagewidth))
                    jp2k_tile = jp2k_tile[:, last_j2k_cols, :].copy()

                if (
                    partial_jp2_tile_rows
                    and stripnum == num_strips - 1
                ):
                    # decrease the number of rows by however many it sticks
                    # over the image height
                    last_j2k_rows = slice(0, imageheight - julr)
                    jp2k_tile = jp2k_tile[last_j2k_rows, :, :].copy()

                tilewriter[:] = jp2k_tile
