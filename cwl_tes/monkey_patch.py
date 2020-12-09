import urllib
import os
import cwltool.argparser
import json
from typing import (
    AnyStr,
    cast
)
from schema_salad.ref_resolver import file_uri


# Patch functions in argparse to enable parsing of s3:// URIs as inputs
def FSActioncall(
    self,
    parser,
    namespace,
    values,
    option_string=None,
):
    url = urllib.parse.urlparse(values)
    if url.scheme == '':
        setattr(
            namespace,
            self.dest,
            {
                "class": self.objclass,
                "location": file_uri(str(
                    os.path.abspath(cast(AnyStr, values)))),
            },
            )
    else:
        setattr(
            namespace,
            self.dest,
            {
                "class": self.objclass,
                "location": values,
            },
            )


cwltool.argparser.FSAction.__call__ = FSActioncall


def FSAppendActioncall(
    self,
    parser,
    namespace,
    values,
    option_string=None,
):

    g = getattr(namespace, self.dest)
    if not g:
        g = []
        setattr(namespace, self.dest, g)
    url = urllib.parse.urlparse(values)
    if url.scheme == "":
        g.append(
            {
                "class": self.objclass,
                "location": file_uri(str(
                    os.path.abspath(cast(AnyStr, values)))),
            }
            )
    else:
        g.append(
            {
                "class": self.objclass,
                "location":  values,
            }
            )


cwltool.argparser.FSAppendAction.__call__ = FSAppendActioncall


def replaceURI(mapper: str, cwl_output: str):
    ''' convert the location of the output from cwltool to the original URI '''
    pathmap = {}

    def getMapper():
        lines = mapper.splitlines()
        for line in lines:
            if line.startswith("Mapper:"):
                dd = line.replace("Mapper: ", "")
                pm_dict = json.loads(dd)
                pathmap[pm_dict['target_uri']] = pm_dict

    getMapper()

    for k in pathmap:
        cwl_output = cwl_output.replace(k, pathmap[k]['resolved'])

    return cwl_output
