# -*- coding: utf-8 -*-

# Copyright 2014-2025 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Downloader module for http:// and https:// URLs"""

import time
import mimetypes
from requests.exceptions import RequestException, ConnectionError, Timeout
from .common import DownloaderBase
from .. import text, util, output, exception
from ssl import SSLError
FLAGS = util.FLAGS


class HttpDownloader(DownloaderBase):
    scheme = "http"

    def __init__(self, job):
        DownloaderBase.__init__(self, job)
        extractor = job.extractor
        self.downloading = False

        self.adjust_extension = self.config("adjust-extensions", True)
        self.chunk_size = self.config("chunk-size", 32768)
        self.metadata = extractor.config("http-metadata")
        self.progress = self.config("progress", 3.0)
        self.validate = self.config("validate", True)
        self.validate_html = self.config("validate-html", True)
        self.headers = self.config("headers")
        self.minsize = self.config("filesize-min")
        self.maxsize = self.config("filesize-max")
        self.retries = self.config("retries", extractor._retries)
        self.retry_codes = self.config("retry-codes", extractor._retry_codes)
        self.timeout = self.config("timeout", extractor._timeout)
        self.verify = self.config("verify", extractor._verify)
        self.mtime = self.config("mtime", True)
        self.rate = self.config("rate")
        interval_429 = self.config("sleep-429")

        if not self.config("consume-content", False):
            # this resets the underlying TCP connection, and therefore
            # if the program makes another request to the same domain,
            # a new connection (either TLS or plain TCP) must be made
            self.release_conn = lambda resp: resp.close()

        if self.retries < 0:
            self.retries = float("inf")
        if self.minsize:
            minsize = text.parse_bytes(self.minsize)
            if not minsize:
                self.log.warning(
                    "Invalid minimum file size (%r)", self.minsize)
            self.minsize = minsize
        if self.maxsize:
            maxsize = text.parse_bytes(self.maxsize)
            if not maxsize:
                self.log.warning(
                    "Invalid maximum file size (%r)", self.maxsize)
            self.maxsize = maxsize
        if isinstance(self.chunk_size, str):
            chunk_size = text.parse_bytes(self.chunk_size)
            if not chunk_size:
                self.log.warning(
                    "Invalid chunk size (%r)", self.chunk_size)
                chunk_size = 32768
            self.chunk_size = chunk_size
        if self.rate:
            func = util.build_selection_func(self.rate, 0, text.parse_bytes)
            if rmax := func.args[1] if hasattr(func, "args") else func():
                if rmax < self.chunk_size:
                    # reduce chunk_size to allow for one iteration each second
                    self.chunk_size = rmax
                self.rate = func
                self.receive = self._receive_rate
            else:
                self.log.warning("Invalid rate limit (%r)", self.rate)
                self.rate = False
        if self.progress is not None:
            self.receive = self._receive_rate
            if self.progress < 0.0:
                self.progress = 0.0
        if interval_429 is None:
            self.interval_429 = extractor._interval_429
        else:
            self.interval_429 = util.build_duration_func(interval_429)

    def download(self, url, pathfmt):
        try:
            return self._download_impl(url, pathfmt)
        except Exception as exc:
            if self.downloading:
                output.stderr_write("\n")
            self.log.debug("", exc_info=exc)
            raise
        finally:
            # remove file from incomplete downloads
            if self.downloading and not self.part:
                util.remove_file(pathfmt.temppath)

    def _download_impl(self, url, pathfmt):
        response = None
        tries = code = 0
        msg = ""

        metadata = self.metadata
        kwdict = pathfmt.kwdict
        expected_status = kwdict.get(
            "_http_expected_status", ())
        adjust_extension = kwdict.get(
            "_http_adjust_extension", self.adjust_extension)

        if self.part and not metadata:
            pathfmt.part_enable(self.partdir)

        while True:
            if tries:
                if response:
                    self.release_conn(response)
                    response = None

                self.log.warning("%s (%s/%s)", msg, tries, self.retries+1)
                if tries > self.retries:
                    return False

                if code == 429 and self.interval_429:
                    s = self.interval_429()
                    time.sleep(s if s > tries else tries)
                else:
                    time.sleep(tries)
                code = 0

            tries += 1
            file_header = None

            # collect HTTP headers
            headers = {"Accept": "*/*"}
            #   file-specific headers
            if extra := kwdict.get("_http_headers"):
                headers.update(extra)
            #   general headers
            if self.headers:
                headers.update(self.headers)
            #   partial content
            if file_size := pathfmt.part_size():
                headers["Range"] = f"bytes={file_size}-"

            # connect to (remote) source
            try:
                response = self.session.request(
                    kwdict.get("_http_method", "GET"), url,
                    stream=True,
                    headers=headers,
                    data=kwdict.get("_http_data"),
                    timeout=self.timeout,
                    proxies=self.proxies,
                    verify=self.verify,
                )
            except ConnectionError as exc:
                try:
                    reason = exc.args[0].reason
                    cls = reason.__class__.__name__
                    pre, _, err = str(reason.args[-1]).partition(":")
                    msg = f"{cls}: {(err or pre).lstrip()}"
                except Exception:
                    msg = str(exc)
                continue
            except Timeout as exc:
                msg = str(exc)
                continue
            except Exception as exc:
                self.log.warning(exc)
                return False

            # check response
            code = response.status_code
            if code == 200 or code in expected_status:  # OK
                offset = 0
                size = response.headers.get("Content-Length")
            elif code == 206:  # Partial Content
                offset = file_size
                size = response.headers["Content-Range"].rpartition("/")[2]
            elif code == 416 and file_size:  # Requested Range Not Satisfiable
                break
            else:
                msg = f"'{code} {response.reason}' for '{url}'"

                challenge = util.detect_challenge(response)
                if challenge is not None:
                    self.log.warning(challenge)

                if code in self.retry_codes or 500 <= code < 600:
                    continue
                retry = kwdict.get("_http_retry")
                if retry and retry(response):
                    continue
                self.release_conn(response)
                self.log.warning(msg)
                return False

            # check for invalid responses
            if self.validate and \
                    (validate := kwdict.get("_http_validate")) is not None:
                try:
                    result = validate(response)
                except Exception:
                    self.release_conn(response)
                    raise
                if isinstance(result, str):
                    url = result
                    tries -= 1
                    continue
                if not result:
                    self.release_conn(response)
                    self.log.warning("Invalid response")
                    return False
            if self.validate_html and response.headers.get(
                    "content-type", "").startswith("text/html") and \
                    pathfmt.extension not in ("html", "htm"):
                if response.history:
                    self.log.warning("HTTP redirect to '%s'", response.url)
                else:
                    self.log.warning("HTML response")
                return False

            # check file size
            size = text.parse_int(size, None)
            if size is not None:
                if self.minsize and size < self.minsize:
                    self.release_conn(response)
                    self.log.warning(
                        "File size smaller than allowed minimum (%s < %s)",
                        size, self.minsize)
                    pathfmt.temppath = ""
                    return True
                if self.maxsize and size > self.maxsize:
                    self.release_conn(response)
                    self.log.warning(
                        "File size larger than allowed maximum (%s > %s)",
                        size, self.maxsize)
                    pathfmt.temppath = ""
                    return True

            build_path = False

            # set missing filename extension from MIME type
            if not pathfmt.extension:
                pathfmt.set_extension(self._find_extension(response))
                build_path = True

            # set metadata from HTTP headers
            if metadata:
                kwdict[metadata] = util.extract_headers(response)
                build_path = True

            # build and check file path
            if build_path:
                pathfmt.build_path()
                if pathfmt.exists():
                    pathfmt.temppath = ""
                    # release the connection back to pool by explicitly
                    # calling .close()
                    # see https://requests.readthedocs.io/en/latest/user
                    # /advanced/#body-content-workflow
                    # when the image size is on the order of megabytes,
                    # re-establishing a TLS connection will typically be faster
                    # than consuming the whole response
                    response.close()
                    return True
                if self.part and metadata:
                    pathfmt.part_enable(self.partdir)
                metadata = False

            content = response.iter_content(self.chunk_size)

            validate_sig = kwdict.get("_http_signature")
            validate_ext = (adjust_extension and
                            pathfmt.extension in SIGNATURE_CHECKS)

            # check filename extension against file header
            if not offset and (validate_ext or validate_sig):
                try:
                    file_header = next(
                        content if response.raw.chunked
                        else response.iter_content(16), b"")
                except (RequestException, SSLError) as exc:
                    msg = str(exc)
                    continue
                if validate_sig:
                    result = validate_sig(file_header)
                    if result is not True:
                        self.release_conn(response)
                        self.log.warning(
                            result or "Invalid file signature bytes")
                        return False
                if validate_ext and self._adjust_extension(
                        pathfmt, file_header) and pathfmt.exists():
                    pathfmt.temppath = ""
                    response.close()
                    return True

            # set open mode
            if not offset:
                mode = "w+b"
                if file_size:
                    self.log.debug("Unable to resume partial download")
            else:
                mode = "r+b"
                self.log.debug("Resuming download at byte %d", offset)

            # download content
            self.downloading = True
            with pathfmt.open(mode) as fp:
                if fp is None:
                    # '.part' file no longer exists
                    break
                if file_header:
                    fp.write(file_header)
                    offset += len(file_header)
                elif offset:
                    if adjust_extension and \
                            pathfmt.extension in SIGNATURE_CHECKS:
                        self._adjust_extension(pathfmt, fp.read(16))
                    fp.seek(offset)

                self.out.start(pathfmt.path)
                try:
                    self.receive(fp, content, size, offset)
                except (RequestException, SSLError) as exc:
                    msg = str(exc)
                    output.stderr_write("\n")
                    continue
                except exception.StopExtraction:
                    response.close()
                    return False
                except exception.ControlException:
                    response.close()
                    raise

                # check file size
                if size and fp.tell() < size:
                    msg = f"file size mismatch ({fp.tell()} < {size})"
                    output.stderr_write("\n")
                    continue

            break

        self.downloading = False
        if self.mtime:
            if "_http_lastmodified" in kwdict:
                kwdict["_mtime_http"] = kwdict["_http_lastmodified"]
            else:
                kwdict["_mtime_http"] = response.headers.get("Last-Modified")
        else:
            kwdict["_mtime_http"] = None

        return True

    def release_conn(self, response):
        """Release connection back to pool by consuming response body"""
        try:
            for _ in response.iter_content(self.chunk_size):
                pass
        except (RequestException, SSLError) as exc:
            output.stderr_write("\n")
            self.log.debug(
                "Unable to consume response body (%s: %s); "
                "closing the connection anyway", exc.__class__.__name__, exc)
            response.close()

    def receive(self, fp, content, bytes_total, bytes_start):
        write = fp.write
        for data in content:
            write(data)

            if FLAGS.DOWNLOAD is not None:
                FLAGS.process("DOWNLOAD")

    def _receive_rate(self, fp, content, bytes_total, bytes_start):
        rate = self.rate() if self.rate else None
        write = fp.write
        progress = self.progress

        bytes_downloaded = 0
        time_start = time.monotonic()

        for data in content:
            time_elapsed = time.monotonic() - time_start
            bytes_downloaded += len(data)

            write(data)

            if FLAGS.DOWNLOAD is not None:
                FLAGS.process("DOWNLOAD")

            if progress is not None:
                if time_elapsed > progress:
                    self.out.progress(
                        bytes_total,
                        bytes_start + bytes_downloaded,
                        int(bytes_downloaded / time_elapsed),
                    )

            if rate is not None:
                time_expected = bytes_downloaded / rate
                if time_expected > time_elapsed:
                    time.sleep(time_expected - time_elapsed)

    def _find_extension(self, response):
        """Get filename extension from MIME type"""
        mtype = response.headers.get("Content-Type", "image/jpeg")
        mtype = mtype.partition(";")[0]

        if "/" not in mtype:
            mtype = "image/" + mtype

        if mtype in MIME_TYPES:
            return MIME_TYPES[mtype]

        if ext := mimetypes.guess_extension(mtype, strict=False):
            return ext[1:]

        self.log.warning("Unknown MIME type '%s'", mtype)
        return "bin"

    def _adjust_extension(self, pathfmt, file_header):
        """Check filename extension against file header"""
        if not SIGNATURE_CHECKS[pathfmt.extension](file_header):
            for ext, check in SIGNATURE_CHECKS.items():
                if check(file_header):
                    pathfmt.set_extension(ext)
                    pathfmt.build_path()
                    return True
        return False


MIME_TYPES = {
    "image/jpeg"    : "jpg",
    "image/jpg"     : "jpg",
    "image/png"     : "png",
    "image/gif"     : "gif",
    "image/bmp"     : "bmp",
    "image/x-bmp"   : "bmp",
    "image/x-ms-bmp": "bmp",
    "image/webp"    : "webp",
    "image/avif"    : "avif",
    "image/heic"    : "heic",
    "image/heif"    : "heif",
    "image/svg+xml" : "svg",
    "image/ico"     : "ico",
    "image/icon"    : "ico",
    "image/x-icon"  : "ico",
    "image/vnd.microsoft.icon" : "ico",
    "image/x-photoshop"        : "psd",
    "application/x-photoshop"  : "psd",
    "image/vnd.adobe.photoshop": "psd",

    "video/webm": "webm",
    "video/ogg" : "ogg",
    "video/mp4" : "mp4",
    "video/m4v" : "m4v",
    "video/x-m4v": "m4v",
    "video/quicktime": "mov",

    "audio/wav"  : "wav",
    "audio/x-wav": "wav",
    "audio/webm" : "webm",
    "audio/ogg"  : "ogg",
    "audio/mpeg" : "mp3",

    "application/zip"  : "zip",
    "application/x-zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/rar"  : "rar",
    "application/x-rar": "rar",
    "application/x-rar-compressed": "rar",
    "application/x-7z-compressed" : "7z",

    "application/pdf"  : "pdf",
    "application/x-pdf": "pdf",
    "application/x-shockwave-flash": "swf",

    "text/html": "html",

    "application/ogg": "ogg",
    # https://www.iana.org/assignments/media-types/model/obj
    "model/obj": "obj",
    "application/octet-stream": "bin",
}


def _signature_html(s):
    s = s[:14].lstrip()
    return s and b"<!doctype html".startswith(s.lower())


# https://en.wikipedia.org/wiki/List_of_file_signatures
SIGNATURE_CHECKS = {
    "jpg" : lambda s: s[0:3] == b"\xFF\xD8\xFF",
    "png" : lambda s: s[0:8] == b"\x89PNG\r\n\x1A\n",
    "gif" : lambda s: s[0:6] in (b"GIF87a", b"GIF89a"),
    "bmp" : lambda s: s[0:2] == b"BM",
    "webp": lambda s: (s[0:4] == b"RIFF" and
                       s[8:12] == b"WEBP"),
    "avif": lambda s: s[4:11] == b"ftypavi" and s[11] in b"fs",
    "heic": lambda s: (s[4:10] == b"ftyphe" and s[10:12] in (
                       b"ic", b"im", b"is", b"ix", b"vc", b"vm", b"vs")),
    "svg" : lambda s: s[0:5] == b"<?xml",
    "ico" : lambda s: s[0:4] == b"\x00\x00\x01\x00",
    "cur" : lambda s: s[0:4] == b"\x00\x00\x02\x00",
    "psd" : lambda s: s[0:4] == b"8BPS",
    "mp4" : lambda s: (s[4:8] == b"ftyp" and s[8:11] in (
                       b"mp4", b"avc", b"iso")),
    "m4v" : lambda s: s[4:11] == b"ftypM4V",
    "mov" : lambda s: s[4:12] == b"ftypqt  ",
    "webm": lambda s: s[0:4] == b"\x1A\x45\xDF\xA3",
    "ogg" : lambda s: s[0:4] == b"OggS",
    "wav" : lambda s: (s[0:4] == b"RIFF" and
                       s[8:12] == b"WAVE"),
    "mp3" : lambda s: (s[0:3] == b"ID3" or
                       s[0:2] in (b"\xFF\xFB", b"\xFF\xF3", b"\xFF\xF2")),
    "zip" : lambda s: s[0:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "rar" : lambda s: s[0:6] == b"Rar!\x1A\x07",
    "7z"  : lambda s: s[0:6] == b"\x37\x7A\xBC\xAF\x27\x1C",
    "pdf" : lambda s: s[0:5] == b"%PDF-",
    "swf" : lambda s: s[0:3] in (b"CWS", b"FWS"),
    "html": _signature_html,
    "htm" : _signature_html,
    "blend": lambda s: s[0:7] == b"BLENDER",
    # unfortunately the Wavefront .obj format doesn't have a signature,
    # so we check for the existence of Blender's comment
    "obj" : lambda s: s[0:11] == b"# Blender v",
    # Celsys Clip Studio Paint format
    # https://github.com/rasensuihei/cliputils/blob/master/README.md
    "clip": lambda s: s[0:8] == b"CSFCHUNK",
    # check 'bin' files against all other file signatures
    "bin" : lambda s: False,
}

__downloader__ = HttpDownloader
