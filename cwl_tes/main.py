"""Main entrypoint for cwl-tes."""
from __future__ import absolute_import, print_function, unicode_literals

import argparse
import os
import functools
import signal
import sys
import logging
import ftplib
import jwt
import uuid
from typing import MutableMapping, MutableSequence
from typing_extensions import Text

import pkg_resources
from six.moves import urllib
from six import itervalues, StringIO

from ruamel import yaml
from schema_salad.sourceline import cmap
from typing import Any, Dict, Tuple, Optional
import cwltool.main
from cwltool.builder import substitute
from cwltool.context import LoadingContext, RuntimeContext
from cwltool.process import scandeps, shortname
from cwltool.executors import (MultithreadedJobExecutor, SingleJobExecutor,
                               JobExecutor)
from cwltool.resolver import ga4gh_tool_registries
from cwltool.pathmapper import visit_class
from cwltool.process import Process

from .tes import make_tes_tool, TESPathMapper
from .__init__ import __version__
from .ftp import FtpFsAccess
from .s3 import S3FsAccess
from .gs import GSFsAccess
from cwltool.stdfsaccess import StdFsAccess

log = logging.getLogger("tes-backend")
log.setLevel(logging.INFO)
console = logging.StreamHandler()
# formatter = logging.Formatter("[%(asctime)s]\t[%(levelname)s]\t%(message)s")
# console.setFormatter(formatter)
log.addHandler(console)

DEFAULT_TMP_PREFIX = "tmp"
DEFAULT_TOKEN_PUBLIC_KEY = os.environ.get('TOKEN_PUBLIC_KEY', '')


def versionstring():
    """Determine our version."""
    pkg = pkg_resources.require("cwltool")
    if pkg:
        cwltool_ver = pkg[0].version
    else:
        cwltool_ver = "unknown"
    return "%s %s with cwltool %s" % (sys.argv[0], __version__, cwltool_ver)


def fs_upload(base_url, fs_access, cwl_obj):
    # type: (Text, FtpFsAccess, Dict[Text, Any]) -> None
    """
    Upload a File or Directory to the given file URL;

    Update the location URL to match.
    """
    if "path" not in cwl_obj and not (
            "location" in cwl_obj and cwl_obj["location"].startswith(
                "file:/")):
        return
    path = cwl_obj.get("path", cwl_obj["location"][6:])
    is_dir = os.path.isdir(path)
    basename = os.path.basename(path)
    dirname = os.path.dirname(path)
    if is_dir and cwl_obj["class"] != "Directory":
        raise ValueError("Passed a directory but Class is not Directory")
    if not is_dir and cwl_obj["class"] != "File":
        raise ValueError("Passed a file but Class is not File")
    try:
        fs_access.mkdir(base_url)
    except ftplib.all_errors:
        pass
    if not fs_access.isdir(base_url):
        raise Exception(
            'Failed to create target directory "{}".'.format(base_url))
    cwl_obj["location"] = base_url + '/' + basename
    cwl_obj.pop("path", None)
    if is_dir:
        if fs_access.isdir(fs_access.join(base_url, basename)):
            log.warning("FS upload, Directory %s already exists", basename)
        else:
            for root, _subdirs, files in os.walk(path, followlinks=True):
                root_path = base_url + '/' + root[len(dirname):]
                fs_access.mkdir(root_path)
                for each_file in files:
                    with open(os.path.join(root,
                                           each_file), mode="rb") as source:
                        fs_access.upload(source, root_path + '/' + each_file)
        cwl_obj.pop("listing", None)
    else:
        if fs_access.isfile(fs_access.join(base_url, basename)):
            log.warning("FTP upload, file %s already exists", basename)
        else:
            with open(path, mode="rb") as source:
                fs_access.upload(source, cwl_obj["location"])

def is_ftp_url(b):
    return b.startswith("ftp:")

def is_s3_url(b):
    return b.startswith("s3:") or b.startswith("s3+http:") or b.startswith("s3+https:")

def is_gs_url(b):
    return b.startswith("gs:")

def main(args=None):
    """Main entrypoint for cwl-tes."""
    if args is None:
        args = sys.argv[1:]

    parser = arg_parser()
    parsed_args = parser.parse_args(args)

    if parsed_args.version:
        print(versionstring())
        return 0

    if parsed_args.tes is None:
        print(versionstring())
        parser.print_usage()
        print("cwl-tes: error: argument --tes is required")
        return 1

    if parsed_args.token:
        try:
            jwt.decode(
                parsed_args.token,
                parsed_args.token_public_key.encode('utf-8')
                .decode('unicode_escape'),
                algorithms=['RS256']
            )
        except Exception:
            raise Exception('Token is not valid')

    if parsed_args.quiet:
        log.setLevel(logging.WARN)
    if parsed_args.debug:
        log.setLevel(logging.DEBUG)

    def signal_handler(*args):  # pylint: disable=unused-argument
        """setup signal handler"""
        log.info(
            "recieved control-c signal"
        )
        log.info(
            "terminating thread(s)..."
        )
        log.warning(
            "remote TES task(s) will keep running"
        )
        sys.exit(1)
    signal.signal(signal.SIGINT, signal_handler)

    ftp_cache = {}
    str_uuid = str(uuid.uuid4())
    class CachingFtpFsAccess(FtpFsAccess):
        """Ensures that the FTP connection cache is shared."""
        def __init__(self, basedir, insecure=False):
            super(CachingFtpFsAccess, self).__init__(
                basedir, ftp_cache, insecure=insecure)

    class CachingS3FsAccess(S3FsAccess):
        """ created only for homogeneity with the FTP counterpart.
            it just returns an instance of S3FsAccess"""
        def __init__(self, basedir, insecure=False, endpoint_url=None):
            super(CachingS3FsAccess, self).__init__(basedir, endpoint_url=endpoint_url)

    # keep as default the original behaviour (ftp)
    fs_access = StdFsAccess(os.curdir)
    # switch to s3 if the remote_storage_url is on s3
    if parsed_args.remote_storage_url:
        if is_ftp_url(parsed_args.remote_storage_url):
            fs_access = CachingFtpFsAccess(os.curdir, insecure=parsed_args.insecure)
        if is_s3_url(parsed_args.remote_storage_url):
            fs_access = CachingS3FsAccess(os.curdir, endpoint_url=parsed_args.endpoint_url)
        if is_gs_url(parsed_args.remote_storage_url):
            fs_access = CachingGSFsAccess(os.curdir)
    if parsed_args.remote_storage_url:
        parsed_args.remote_storage_url = fs_access.join(
            parsed_args.remote_storage_url, str_uuid)
    loading_context = cwltool.main.LoadingContext(vars(parsed_args))
    loading_context.construct_tool_object = functools.partial(
        make_tes_tool, url=parsed_args.tes,
        remote_storage_url=parsed_args.remote_storage_url,
        token=parsed_args.token)
    runtime_context = cwltool.main.RuntimeContext(vars(parsed_args))

    if parsed_args.remote_storage_url:
        if is_s3_url(parsed_args.remote_storage_url):
            runtime_context.make_fs_access = functools.partial(
            CachingS3FsAccess, insecure=parsed_args.insecure, endpoint_url=parsed_args.endpoint_url)
        elif is_ftp_url(parsed_args.remote_storage_url):
            runtime_context.make_fs_access = functools.partial(
            CachingFtpFsAccess, insecure=parsed_args.insecure)
        elif is_gs_url(parsed_args.remote_storage_url):
            runtime_context.make_fs_access = functools.partial(
            CachingGSFsAccess)
        else:
            raise Exception("Unknown Schema")

    runtime_context.path_mapper = functools.partial(
        TESPathMapper, fs_access=fs_access)
    job_executor = MultithreadedJobExecutor() if parsed_args.parallel \
        else SingleJobExecutor()
    job_executor.max_ram = job_executor.max_cores = float("inf")
    executor = functools.partial(
        tes_execute, job_executor=job_executor,
        loading_context=loading_context,
        remote_storage_url=parsed_args.remote_storage_url,
        fs_access=fs_access)
    runtime_context.str_uuid = str_uuid
    return cwltool.main.main(
        args=parsed_args,
        executor=executor,
        loadingContext=loading_context,
        runtimeContext=runtime_context,
        versionfunc=versionstring,
        logger_handler=console
    )


def tes_execute(process,           # type: Process
                job_order,         # type: Dict[Text, Any]
                runtime_context,   # type: RuntimeContext
                job_executor,      # type: JobExecutor
                loading_context,   # type: LoadingContext
                remote_storage_url,
                fs_access,
                logger=log
                ):  # type: (...) -> Tuple[Optional[Dict[Text, Any]], Text]
    """
    Upload to the remote_storage_url (if needed) and execute.

    Adapted from:
    https://github.com/curoverse/arvados/blob/2b0b06579199967eca3d44d955ad64195d2db3c3/sdk/cwl/arvados_cwl/__init__.py#L407
    """
    if remote_storage_url:
        upload_workflow_deps(process, remote_storage_url, fs_access)
        # Reload tool object which may have been updated by
        # upload_workflow_deps
        # Don't validate this time because it will just print redundant errors.
        loading_context = loading_context.copy()
        loading_context.loader = process.doc_loader
        loading_context.avsc_names = process.doc_schema
        loading_context.metadata = process.metadata
        loading_context.do_validate = False
        process = loading_context.construct_tool_object(
            process.doc_loader.idx[process.tool["id"]], loading_context)
        job_order = upload_job_order(
            process, job_order, remote_storage_url, fs_access)

    if not job_executor:
        job_executor = MultithreadedJobExecutor()
    return job_executor(process, job_order, runtime_context, logger)


def upload_workflow_deps(process, remote_storage_url, fs_access):
    """
    Ensure that all default files in this workflow are uploaded.

    Adapted from:
    https://github.com/curoverse/arvados/blob/2b0b06579199967eca3d44d955ad64195d2db3c3/sdk/cwl/arvados_cwl/runner.py#L292
    """
    document_loader = process.doc_loader

    def upload_tool_deps(deptool):
        if "id" in deptool:
            upload_dependencies(document_loader, deptool, deptool["id"],
                                    True, remote_storage_url, fs_access)
            document_loader.idx[deptool["id"]] = deptool
    process.visit(upload_tool_deps)


def upload_dependencies(document_loader, workflowobj, uri, loadref_run,
                            remote_storage_url, fs_access):
    """
    Upload the dependencies of the workflowobj document to an storage location.

    Does an in-place update of references in "workflowobj".
    Use scandeps to find $import, $include, $schemas, run, File and Directory
    fields that represent external references.
    If workflowobj has an "id" field, this will reload the document to ensure
    it is scanning the raw document prior to preprocessing.

    Adapted from:
    https://github.com/curoverse/arvados/blob/2b0b06579199967eca3d44d955ad64195d2db3c3/sdk/cwl/arvados_cwl/runner.py#L83
    """
    loaded = set()

    def loadref(base, ref):
        joined = document_loader.fetcher.urljoin(base, ref)
        defrg, _ = urllib.parse.urldefrag(joined)
        if defrg not in loaded:
            loaded.add(defrg)
            # Use fetch_text to get raw file (before preprocessing).
            text = document_loader.fetch_text(defrg)
            if isinstance(text, bytes):
                text_io = StringIO(text.decode('utf-8'))
            else:
                text_io = StringIO(text)
            return yaml.safe_load(text_io)
        else:
            return {}
    if loadref_run:
        loadref_fields = set(("$import", "run"))
    else:
        loadref_fields = set(("$import",))
    scanobj = workflowobj
    if "id" in workflowobj:
        # Need raw file content (before preprocessing) to ensure
        # that external references in $include and $mixin are captured.
        scanobj = loadref("", workflowobj["id"])
    scandeps(uri, scanobj, loadref_fields,
             set(("$include", "$schemas", "location")),
             loadref, urljoin=document_loader.fetcher.urljoin)

    def visit_default(obj):
        remove = [False]

        def ensure_default_location(fileobj):
            if "location" not in fileobj and "path" in fileobj:
                fileobj["location"] = fileobj["path"]
                del fileobj["path"]
            if "location" in fileobj \
                    and not fs_access.exists(fileobj["location"]):
                # Delete "default" from workflowobj
                remove[0] = True
        visit_class(obj["default"], ("File", "Directory"),
                    ensure_default_location)
        if remove[0]:
            del obj["default"]
    find_defaults(workflowobj, visit_default)

    discovered = {}

    def discover_default_secondary_files(obj):
        discover_secondary_files(
            obj["inputs"],
            {shortname(t["id"]): t["default"] for t in obj["inputs"]
             if "default" in t},
            discovered)

    visit_class(workflowobj, ("CommandLineTool", "Workflow"),
                discover_default_secondary_files)
    for entry in list(discovered.keys()):
        # Only interested in discovered secondaryFiles which are local
        # files that need to be uploaded.
        if not entry.startswith("file:"):
            del discovered[entry]
    visit_class(workflowobj, ("Directory"), functools.partial(
        fs_upload, remote_storage_url, fs_access))
    visit_class(workflowobj, ("File"), functools.partial(
        fs_upload, remote_storage_url, fs_access))
    visit_class(discovered, ("Directory"), functools.partial(
        fs_upload, remote_storage_url, fs_access))
    visit_class(discovered, ("File"), functools.partial(
        fs_upload, remote_storage_url, fs_access))


def find_defaults(item, operation):
    """
    Find instances of a default field and apply the given operation.

    Adapted from:
    https://github.com/curoverse/arvados/blob/2b0b06579199967eca3d44d955ad64195d2db3c3/sdk/cwl/arvados_cwl/runner.py#L56
    """
    if isinstance(item, MutableSequence):
        for entry in item:
            find_defaults(entry, operation)
    elif isinstance(item, MutableMapping):
        if "default" in item:
            operation(item)
        else:
            for entry in itervalues(item):
                find_defaults(entry, operation)


def discover_secondary_files(inputs, job_order, discovered=None):
    """
    Find secondaryFiles in the schema and transfer to the job_order.

    Adapted from:
    https://github.com/curoverse/arvados/blob/2b0b06579199967eca3d44d955ad64195d2db3c3/sdk/cwl/arvados_cwl/runner.py#L166
    """
    for typedef in inputs:
        if shortname(typedef["id"]) in job_order \
                and typedef.get("secondaryFiles"):
            set_secondary(typedef, job_order[shortname(typedef["id"])],
                          discovered)


def set_secondary(typedef, fileobj, discovered):
    """
    Pull over missing secondaryFiles to the job object entry.

    Adapted from:
    https://github.com/curoverse/arvados/blob/2b0b06579199967eca3d44d955ad64195d2db3c3/sdk/cwl/arvados_cwl/runner.py#L67
    """
    if isinstance(fileobj, MutableMapping) and fileobj.get("class") == "File":
        if "secondaryFiles" not in fileobj:
            fileobj["secondaryFiles"] = cmap(
                [{"location": substitute(fileobj["location"], sf["pattern"]),
                  "class": "File"} for sf in typedef["secondaryFiles"]])
            if discovered is not None:
                discovered[fileobj["location"]] = fileobj["secondaryFiles"]
    elif isinstance(fileobj, MutableSequence):
        for entry in fileobj:
            set_secondary(typedef, entry, discovered)


def upload_job_order(process, job_order, remote_storage_url, fs_access):
    """
    Upload local files referenced in the input object and return updated input
    object with 'location' updated to new URIs.

    Adapted from:
    https://github.com/curoverse/arvados/blob/2b0b06579199967eca3d44d955ad64195d2db3c3/sdk/cwl/arvados_cwl/runner.py#L266
    """
    discover_secondary_files(process.tool["inputs"], job_order)
    upload_dependencies(process.doc_loader, job_order,
                            job_order.get("id", "#"), False,
                            remote_storage_url, fs_access)
    if "id" in job_order:
        del job_order["id"]
    # Need to filter this out, gets added by cwltool when providing
    # parameters on the command line.
    if "job_order" in job_order:
        del job_order["job_order"]
    return job_order


def arg_parser():  # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser(
        description='GA4GH TES executor for Common Workflow Language.')
    parser.add_argument("--tes", type=str, help="GA4GH TES Service URL.")
    parser.add_argument("--basedir", type=Text)
    parser.add_argument("--outdir",
                        type=Text, default=os.path.abspath('.'),
                        help="Output directory, default current directory")
    parser.add_argument("--remote-storage-url", type=str)
    parser.add_argument("--endpoint-url", type=str)
    parser.add_argument("--insecure", action="store_true",
                        help=("Connect securely to FTP server (ignored when "
                              "--remote-storage-url is not set)"))
    parser.add_argument("--token", type=str)
    parser.add_argument("--token-public-key", type=str,
                        default=DEFAULT_TOKEN_PUBLIC_KEY)
    envgroup = parser.add_mutually_exclusive_group()
    envgroup.add_argument(
        "--preserve-environment",
        type=Text,
        action="append",
        help="Preserve specific environment variable when "
        "running CommandLineTools.  May be provided multiple "
        "times.",
        metavar="ENVVAR",
        default=[],
        dest="preserve_environment")
    envgroup.add_argument(
        "--preserve-entire-environment",
        action="store_true",
        help="Preserve all environment variable when running "
        "CommandLineTools.",
        default=False,
        dest="preserve_entire_environment")

    parser.add_argument("--tmpdir-prefix", type=Text,
                        help="Path prefix for temporary directories",
                        default=DEFAULT_TMP_PREFIX)

    exgroup = parser.add_mutually_exclusive_group()
    exgroup.add_argument(
        "--tmp-outdir-prefix",
        type=Text,
        help="Path prefix for intermediate output directories",
        default=DEFAULT_TMP_PREFIX)

    exgroup.add_argument(
        "--cachedir",
        type=Text,
        default="",
        help="Directory to cache intermediate workflow outputs to avoid "
        "recomputing steps."
    )

    exgroup = parser.add_mutually_exclusive_group()
    exgroup.add_argument(
        "--rm-tmpdir",
        action="store_true",
        default=True,
        help="Delete intermediate temporary directories (default)",
        dest="rm_tmpdir")

    exgroup.add_argument(
        "--leave-tmpdir",
        action="store_false",
        default=True,
        help="Do not delete intermediate temporary directories",
        dest="rm_tmpdir")

    exgroup = parser.add_mutually_exclusive_group()
    exgroup.add_argument(
        "--move-outputs",
        action="store_const",
        const="move",
        default="move",
        help="Move output files to the workflow output directory and delete "
        "intermediate output directories (default).",
        dest="move_outputs")

    exgroup.add_argument(
        "--leave-outputs",
        action="store_const",
        const="leave",
        default="move",
        help="Leave output files in intermediate output directories.",
        dest="move_outputs")

    exgroup.add_argument(
        "--copy-outputs",
        action="store_const",
        const="copy",
        default="move",
        help="Copy output files to the workflow output directory, don't "
        "delete intermediate output directories.",
        dest="move_outputs")

    parser.add_argument(
        "--rdf-serializer",
        help="Output RDF serialization format used by --print-rdf (one of "
        "turtle (default), n3, nt, xml)",
        default="turtle")

    parser.add_argument(
        "--eval-timeout",
        help="Time to wait for a Javascript expression to evaluate before "
        "giving an error, default 20s.",
        type=float,
        default=20)

    exgroup = parser.add_mutually_exclusive_group()
    exgroup.add_argument(
        "--print-rdf",
        action="store_true",
        help="Print corresponding RDF graph for workflow and exit")
    exgroup.add_argument(
        "--print-dot",
        action="store_true",
        help="Print workflow visualization in graphviz format and exit")
    exgroup.add_argument(
        "--print-pre",
        action="store_true",
        help="Print CWL document after preprocessing.")
    exgroup.add_argument(
        "--print-deps",
        action="store_true",
        help="Print CWL document dependencies.")
    exgroup.add_argument(
        "--print-input-deps",
        action="store_true",
        help="Print input object document dependencies.")
    exgroup.add_argument(
        "--pack",
        action="store_true",
        help="Combine components into single document and print.")
    exgroup.add_argument(
        "--version",
        action="store_true",
        help="Print version and exit")
    exgroup.add_argument(
        "--validate",
        action="store_true",
        help="Validate CWL document only.")
    exgroup.add_argument(
        "--print-supported-versions",
        action="store_true",
        help="Print supported CWL specs.")

    exgroup = parser.add_mutually_exclusive_group()
    exgroup.add_argument(
        "--strict",
        action="store_true",
        help="Strict validation (unrecognized or out of place fields are "
        "error)",
        default=True,
        dest="strict")
    exgroup.add_argument(
        "--non-strict",
        action="store_false",
        help="Lenient validation (ignore unrecognized fields)",
        default=True,
        dest="strict")

    parser.add_argument(
        "--skip-schemas",
        action="store_true",
        help="Skip loading of schemas",
        default=False,
        dest="skip_schemas")

    exgroup = parser.add_mutually_exclusive_group()
    exgroup.add_argument(
        "--verbose", action="store_true", help="Default logging")
    exgroup.add_argument(
        "--quiet",
        action="store_true",
        help="Only print warnings and errors.")
    exgroup.add_argument(
        "--debug",
        action="store_true",
        help="Print even more logging")

    parser.add_argument(
        "--timestamps",
        action="store_true",
        help="Add timestamps to the errors, warnings, and notifications.")
    parser.add_argument(
        "--js-console",
        action="store_true",
        help="Enable javascript console output")
    parser.add_argument("--disable-js-validation",
                        action="store_true",
                        help="Disable javascript validation.")
    parser.add_argument("--js-hint-options-file",
                        type=Text,
                        help="File of options to pass to jshint."
                        "This includes the added option \"includewarnings\". ")

    parser.add_argument(
        "--tool-help",
        action="store_true",
        help="Print command line help for tool")

    parser.add_argument(
        "--relative-deps",
        choices=[
            'primary',
            'cwd'],
        default="primary",
        help="When using --print-deps, print paths "
        "relative to primary file or current working directory.")

    parser.add_argument(
        "--enable-dev",
        action="store_true",
        help="Enable loading and running development versions of CWL spec.",
        default=False)

    parser.add_argument(
        "--enable-ext",
        action="store_true",
        help="Enable loading and running cwltool extensions to CWL spec.",
        default=False)

    parser.add_argument(
        "--default-container",
        help="Specify a default docker container that will be used if the "
        "workflow fails to specify one.")
    parser.add_argument("--disable-validate", dest="do_validate",
                        action="store_false", default=True,
                        help=argparse.SUPPRESS)

    exgroup = parser.add_mutually_exclusive_group()
    exgroup.add_argument(
        "--enable-ga4gh-tool-registry",
        action="store_true",
        help="Enable resolution using GA4GH tool registry API",
        dest="enable_ga4gh_tool_registry",
        default=True)
    exgroup.add_argument(
        "--disable-ga4gh-tool-registry",
        action="store_false",
        help="Disable resolution using GA4GH tool registry API",
        dest="enable_ga4gh_tool_registry",
        default=True)

    parser.add_argument(
        "--add-ga4gh-tool-registry",
        action="append",
        help="Add a GA4GH tool registry endpoint to use for resolution, "
        "default %s" % ga4gh_tool_registries,
        dest="ga4gh_tool_registries",
        default=[])

    parser.add_argument(
        "--on-error",
        help="Desired workflow behavior when a step fails. "
        "One of 'stop' or 'continue'. Default is 'stop'.",
        default="stop",
        choices=("stop", "continue"))

    exgroup = parser.add_mutually_exclusive_group()
    exgroup.add_argument(
        "--compute-checksum",
        action="store_true",
        default=False,
        help="Compute checksum of contents while collecting outputs",
        dest="compute_checksum")
    exgroup.add_argument(
        "--no-compute-checksum",
        action="store_false",
        help="Do not compute checksum of contents while collecting outputs",
        dest="compute_checksum")

    parser.add_argument(
        "--relax-path-checks",
        action="store_true",
        default=False,
        help="Relax requirements on path names to permit "
        "spaces and hash characters.",
        dest="relax_path_checks")
    parser.add_argument("--make-template",
                        action="store_true",
                        help="Generate a template input object")
    parser.add_argument(
        "--overrides",
        type=str,
        default=None,
        help="Read process requirement overrides from file.")
    exgroup = parser.add_mutually_exclusive_group()
    exgroup.add_argument(
        "--parallel", action="store_true", default=True,
        help="Run jobs in parallel (the default)")
    exgroup.add_argument(
        "--serial", action="store_false", dest="parallel",
        help="Run jobs in parallel (the default)")

    parser.add_argument(
        "workflow",
        type=Text,
        nargs="?",
        default=None,
        metavar='cwl_document',
        help="path or URL to a CWL Workflow, "
        "CommandLineTool, or ExpressionTool. If the `inputs_object` has a "
        "`cwl:tool` field indicating the path or URL to the cwl_document, "
        " then the `workflow` argument is optional.")
    parser.add_argument(
        "job_order",
        nargs=argparse.REMAINDER,
        metavar='inputs_object',
        help="path or URL to a YAML or JSON "
        "formatted description of the required input values for the given "
        "`cwl_document`.")

    return parser


if __name__ == "__main__":
    sys.exit(main())
