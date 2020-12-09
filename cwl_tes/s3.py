"""AWS S3 support"""
from __future__ import absolute_import

import fnmatch
# import ftplib
import glob
import boto3
import os
from typing import List, Text  # noqa F401 # pylint: disable=unused-import

import logging

from six.moves import urllib
# from typing import Tuple, Optional
# import re
from cwltool.stdfsaccess import StdFsAccess
from .ftp import abspath

from .s3file import S3File

log = logging.getLogger("tes-backend")

class S3FsAccess(StdFsAccess):
    """S3 access with upload."""
    def __init__(self, basedir, cache=None, insecure=False, endpoint_url=None):  # type: (Text) -> None
        super(S3FsAccess, self).__init__(basedir)
        log.info("Initializing S3Access object")
        self.cache = cache or {}
        self.uuid = None
        self.endpoint_url = endpoint_url

    def _parse_url(self, url):
        parse = urllib.parse.urlparse(url)
        if parse.scheme in ["s3+http", "s3+https"]:
            tmp = parse.path.split("/")
            if len(tmp[0]) == 0:
                tmp.pop(0)
            bucket = tmp[0]
            path = "/".join(tmp[1:])
            endpoint = parse.scheme.split("+")[1] + "://" + parse.netloc
            return endpoint, bucket, path
        else:
            bucket = parse.netloc
            path = parse.path
            if path[0] == '/':
                path = path[1:]
            return self.endpoint_url, bucket, path

    def _connect(self, url):
        '''caches and returns the s3 connection '''
        parse = urllib.parse.urlparse(url)
        if parse.scheme in ["s3", "s3+http", "s3+https"]:
            endpoint, bucketname, path = self._parse_url(url)
            if (bucketname) in self.cache:
                return self.cache[(bucketname)]
            session = boto3.session.Session()
            self.cache[(bucketname)] = session
            return session
        return None

    def setUUID(self, uuid):
        self.uuid = uuid

    def getUUID(self):
        return(self.uuid)

    def _abs(self, p):
        return abspath(p, self.basedir)

    def glob(self, pattern):
        if not self.basedir.startswith("s3:"):
            return super(S3FsAccess, self).glob(pattern)
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
        if not fn.startswith("s3:"):
            return super(S3FsAccess, self).open(fn, mode)
        if 'r' in mode:
            endpoint, bucket, path = self._parse_url(fn)
            s3 = boto3.resource("s3", endpoint_url=endpoint)
            s3_object = s3.Object(bucket_name=bucket, key=path)
            return S3File(s3_object)
        if 'w' in mode:
            # check if file exists
            if self.exists(fn):
                raise Exception(
                    'Cannot override or append s3 objects. {} exists.'.
                    format(fn))
            bucket, path = self._parse_url(fn)
            s3 = boto3.resource("s3")
            s3_object = s3.Object(bucket_name=bucket, key=path)
            return S3File(s3_object)
        raise Exception('{} mode s3 not implemented'.format(mode))

    def exists(self, fn):  # type: (Text) -> bool
        s3session = self._connect(fn)
        if s3session:
            return self.isfile(fn) or self.isdir(fn)
        return super(S3FsAccess, self).exists(fn)

    def isfile(self, fn):  # type: (Text) -> bool
        s3session = self._connect(fn)
        if s3session:
            try:
                sz = self.size(fn)
                if sz is None:
                    return False
                return True
            except Exception as e:
                return False
        return super(S3FsAccess, self).isfile(fn)

    def isdir(self, fn):
        '''
        Check if a bucket resource exists and is a directory.
        Note: S3 does not really distinguish between files and directory.
        Consequently, this function (and `isfile`) merely check that the
        given object exists.
        '''
        s3session = self._connect(fn)
        if s3session:
            endpoint, bucketname, path = self._parse_url(fn)
            s3bucket = s3session.client('s3', endpoint_url=endpoint)
            if path[0] == '/':
                path = path[1:]
            if path[-1] == '/':
                path = path[:-1]
            resp = s3bucket.list_objects_v2(
                        Bucket=bucketname,
                        Prefix=path)
            count = 0
            print("IsDir ", fn, resp)
            if "Contents" in resp:
                for elem in resp["Contents"]:
                    if elem["Key"].startswith(path + "/"):
                        count += 1
            return count > 0
        return super(S3FsAccess, self).isdir(fn)

    def mkdir(self, url, recursive=True):
        """Make the directory specified in the URL.
           For s3 it just creates an ojbect that ends with /"""
        s3session = self._connect(url)
        endpoint, bucketname, path = self._parse_url(url)
        if path[0] == '/':
            path = path[1:]
        if path[-1] != '/':
            path = path + '/'
        try:
            s3bucket = s3session.client('s3', endpoint_url=endpoint)
            resp = s3bucket.put_object(Bucket=bucketname, Key=path)
            return True
        except Exception as e:
            return False
        return None

    def listdir(self, fn):  # type: (Text) -> List[Text]
        s3session = self._connect(fn)
        if s3session:
            endpoint, bucketname, path = self._parse_url(fn)
            s3bucket = s3session.client('s3', endpoint_url=endpoint)
            if path[0] == '/':
                path = path[1:]
            if path[-1] != '/':
                path = path+'/'
            return ["s3://{}/{}".format(bucketname, x['Key'])
                    for x in s3bucket.list_objects_v2(
                        Bucket=bucketname,
                        Prefix=path)['Contents']]
        return super(S3FsAccess, self).listdir(fn)

    def join(self, path, *paths):
        if path.startswith('s3:'):
            result = path
            for extra_path in paths:
                if extra_path.startswith('s3:/'):
                    result = extra_path
                else:
                    if result[-1] == '/':
                        result = result[:-1]
                    result = result + "/" + extra_path
            return result
        return super(S3FsAccess, self).join(path, *paths)

    def realpath(self, path):
        if path.startswith('s3:'):
            return path
        return os.path.realpath(path)

    def size(self, fn):
        s3session = self._connect(fn)
        if s3session:
            endpoint, bucketname, path = self._parse_url(fn)
            try:
                s3bucket = s3session.client('s3', endpoint_url=endpoint)
                obj = s3bucket.head_object(Bucket=bucketname, Key=path)
                size = obj['ContentLength']
                print("Size", fn, obj)
                return size
            except Exception as e:
                return None
        return super(S3FsAccess, self).size(fn)

    def upload(self, file_handle, url):
        """s3 specific method to upload a file to the given URL."""
        s3session = self._connect(url)
        #try:
        endpoint, bucketname, path = self._parse_url(url)
        s3bucket = s3session.client('s3', endpoint_url=endpoint)
        s3bucket.upload_fileobj(
            Fileobj=file_handle,
            Bucket=bucketname,
            Key=path)
        #except Exception as e:
        #    raise Exception("Cannot store file {} to {}.{}".
        #                    format(file_handle, path, e))
