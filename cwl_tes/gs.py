"""Google Storage Support"""

import io
import logging

from google.cloud.storage import client

from cwltool.stdfsaccess import StdFsAccess

log = logging.getLogger("tes-backend")


class GSFsAccess(StdFsAccess):

    """Google Storage access with upload."""
    def __init__(self, basedir, cache=None, insecure=False, endpoint_url=None):  # type: (Text) -> None
        super(GSFsAccess, self).__init__(basedir)
        log.info("Initializing GSFsAccess object")
        self.cache = cache or {}
        self.uuid = None
        self.endpoint_url = endpoint_url

    def setUUID(self, uuid):
        self.uuid = uuid

    def getUUID(self):
        return(self.uuid)

    def _parse_url(self, url):
        parse = urllib.parse.urlparse(url)
        bucket = parse.netloc
        path = parse.path
        if path[0] == '/':
            path = path[1:]
        return bucket, path

    def glob(self, pattern):
        if not self.basedir.startswith("gs:"):
            return super(GSFsAccess, self).glob(pattern)
        return self._glob(pattern)

    def _glob0(self, basename, basepath):
        if basename == '':
            if self.isdir(basepath):
                return [basename]
        else:
            if self.isfile(self.join(basepath, basename)):
                return [basename]
        return []

    def _glob1(self, pattern, basepath=None):
        try:
            names = self.listdir(basepath)
        except Exception as e:
            return []
        if pattern[0] != '.':
            names = filter(lambda x: x[0] != '.', names)
        return fnmatch.filter(names, pattern)

    def _glob(self, pattern):  # type: (Text) -> List[Text]
        if pattern.endswith("/."):
            pattern = pattern[:-1]
        dirname, basename = pattern.rsplit('/', 1)
        if not glob.has_magic(pattern):
            if basename:
                if self.exists(pattern):
                    return [pattern]
            else:  # Patterns ending in slash should match only directories
                if self.isdir(dirname):
                    return [pattern]
            return []
        if not dirname:
            return self._glob1(basename)

        dirs = self._glob(dirname)
        if glob.has_magic(basename):
            glob_in_dir = self._glob1
        else:
            glob_in_dir = self._glob0
        results = []
        for dirname in dirs:
            results.extend(glob_in_dir(basename, dirname))
        return results

    def open(self, fn, mode):
        if not fn.startswith("gs:"):
            return super(GSFsAccess, self).open(fn, mode)
        if 'r' in mode:
            bucket_name, path = self._parse_url(fn)
            storage_client = storage.Client()
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(path)
            return GSFile(blob)
        if 'w' in mode:
            # check if file exists
            if self.exists(fn):
                raise Exception(
                    'Cannot override or append GS objects. {} exists.'.
                    format(fn))
            bucket_name, path = self._parse_url(fn)
            storage_client = storage.Client()
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(path)
            return GSFile(blob)
        raise Exception('{} mode gs not implemented'.format(mode))

    def exists(self, fn):  # type: (Text) -> bool
        if not fn.startswith("gs:"):
            return super(GSFsAccess, self).exists(fn)
        bucket_name, path = self._parse_url(fn)
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(path)
        return blob.exists()

    def isfile(self, fn):  # type: (Text) -> bool
        if not fn.startswith("gs:"):
            return super(GSFsAccess, self).isfile(fn)
        bucket_name, path = self._parse_url(fn)
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(path)
        return blob.exists()

    def isdir(self, fn):
        if not fn.startswith("gs:"):
            return super(GSFsAccess, self).isdir(fn)
        bucket_name, path = self._parse_url(url)
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(path)

        if path[0] == '/':
            path = path[1:]
        if path[-1] == '/':
            path = path[:-1]

        count = 0
        for b in bucket.list_blobs(prefix=path):
            if b.name.startswith(path + "/"):
                count += 1
        return count > 0

    def mkdir(self, url, recursive=True):
        if not fn.startswith("gs:"):
            return super(GSFsAccess, self).mkdir(url, recursive)

        bucket_name, path = self._parse_url(url)
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(path)

        if path[0] == '/':
            path = path[1:]
        if path[-1] != '/':
            path = path + '/'
        blob.upload_from_string('', content_type='application/x-www-form-urlencoded;charset=UTF-8')
        return True

    def listdir(self, fn):  # type: (Text) -> List[Text]
        if not fn.startswith("gs:"):
            return super(GSFsAccess, self).listdir(fn)
        bucket_name, path = self._parse_url(url)
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        if path[0] == '/':
            path = path[1:]
        if path[-1] != '/':
            path = path+'/'
        return ["gs://{}/{}".format(bucket_name, x.name)
                    for x in bucket.list_blobs(prefix=path)]

    def join(self, path, *paths):
        if not fn.startswith("gs:"):
            return super(GSFsAccess, self).join(path, paths)

        result = path
        for extra_path in paths:
            if extra_path.startswith('gs:/'):
                result = extra_path
            else:
                if result[-1] == '/':
                    result = result[:-1]
                result = result + "/" + extra_path
        return result

    def realpath(self, path):
        if path.startswith('gs:'):
            return path
        return os.path.realpath(path)

    def size(self, fn):
        if not fn.startswith("gs:"):
            return super(GSFsAccess, self).join(path, paths)
        bucket_name, path = self._parse_url(url)
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(path)
        return blob.size

    def upload(self, file_handle, url):
        """GS specific method to upload a file to the given URL."""
        bucket_name, path = self._parse_url(url)
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(path)
        blob.upload_from_file(file_handle)

class GSFile(io.RawIOBase):
    def __init__(self, gs_blob):
        self.gs_blob = gs_blob_object
        self.position = 0

    def __repr__(self):
        return "<%s gs_blob=%r>" % (type(self).__name__, self.gs_blob)

    @property
    def size(self):
        return self.gs_blob.size

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.position = offset
        elif whence == io.SEEK_CUR:
            self.position += offset
        elif whence == io.SEEK_END:
            self.position = self.size + offset
        else:
            raise ValueError("invalid whence (%r, should be %d, %d, %d)" % (
                whence, io.SEEK_SET, io.SEEK_CUR, io.SEEK_END
            ))

        return self.position

    def seekable(self):
        return True

    def read(self, size=-1):
        if self.position >= self.size:
            return ''
        if size == -1:
            range_header = "bytes=%d-" % self.position
            self.seek(offset=0, whence=io.SEEK_END)
        else:
            new_position = self.position + size
            if new_position >= self.size:
                return self.read()

            range_header = "bytes=%d-%d" % (self.position, new_position - 1)
            self.seek(offset=size, whence=io.SEEK_CUR)
        return self.gs_blob.download_as_bytes(start=self.position, end=new_position)

    def write(self, content):
        self.gs_blob.upload_from_string(content)
        return True

    def readable(self):
        return True
