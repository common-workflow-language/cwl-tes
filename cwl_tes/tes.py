from __future__ import absolute_import, print_function, unicode_literals

import logging
import os
import random
import time
from builtins import str
import shutil
import functools
from pprint import pformat
from typing import Any, Dict, List, Text, Union

import tes

from cwltool.command_line_tool import CommandLineTool
from cwltool.errors import WorkflowException, UnsupportedRequirement
from cwltool.job import JobBase
from cwltool.stdfsaccess import StdFsAccess
from cwltool.utils import onWindows
from cwltool.workflow import default_make_tool
from schema_salad.ref_resolver import file_uri

log = logging.getLogger("tes-backend")


def make_tes_tool(spec, loading_context, url):
    if "class" in spec and spec["class"] == "CommandLineTool":
        return TESCommandLineTool(spec, loading_context, url)
    else:
        return default_make_tool(spec, loading_context)


class TESCommandLineTool(CommandLineTool):

    def __init__(self, spec, loading_context, url):
        super(TESCommandLineTool, self).__init__(spec, loading_context)
        self.spec = spec
        self.url = url

    def make_job_runner(self, runtime_context):
        return functools.partial(TESTask, runtime_context=runtime_context,
                                 url=self.url, spec=self.spec)


class TESTask(JobBase):
    JobOrderType = Dict[Text, Union[Dict[Text, Any], List, Text]]

    def __init__(self,
                 builder,   # type: Builder
                 joborder,  # type: JobOrderType
                 make_path_mapper,  # type: Callable[..., PathMapper]
                 requirements,  # type: List[Dict[Text, Text]]
                 hints,  # type: List[Dict[Text, Text]]
                 name,   # type: Text
                 runtime_context,
                 url,
                 spec):
        super(TESTask, self).__init__(builder, joborder, make_path_mapper,
                                      requirements, hints, name)
        self.runtime_context = runtime_context
        self.spec = spec
        self.outputs = None
        self.inplace_update = False
        if runtime_context.basedir is not None:
            self.basedir = runtime_context.basedir
        else:
            self.basedir = os.getcwd()
        self.fs_access = StdFsAccess(self.basedir)

        self.id = None
        self.docker_workdir = '/var/spool/cwl'
        self.state = "UNKNOWN"
        self.poll_interval = 1
        self.poll_retries = 10
        self.client = tes.HTTPClient(url)

    def get_container(self):
        default = "python:2.7"
        container = default
        if self.runtime_context.default_container:
            container = self.runtime_context.default_container

        reqs = self.spec.get("requirements", []) + self.spec.get("hints", [])
        for i in reqs:
            if i.get("class", "NA") == "DockerRequirement":
                container = i.get(
                    "dockerPull",
                    i.get("dockerImageId", default)
                )
        return container

    def create_input(self, name, d):
        if "contents" in d:
            return tes.Input(
                name=name,
                description="cwl_input:%s" % (name),
                path=d["path"],
                content=d["contents"],
                type=d["class"].upper()
            )
        else:
            return tes.Input(
                name=name,
                description="cwl_input:%s" % (name),
                url=d["location"],
                path=d["path"],
                type=d["class"].upper()
            )

    def parse_job_order(self, k, v, inputs):
        if isinstance(v, dict):
            if all([i in v for i in ["location", "path", "class"]]):
                inputs.append(self.create_input(k, v))

                if "secondaryFiles" in v:
                    for f in v["secondaryFiles"]:
                        self.parse_job_order(f["basename"], f, inputs)

            else:
                for sk, sv in v.items():
                    if isinstance(sv, dict):
                        self.parse_job_order(sk, sv, inputs)

                    else:
                        break

        elif isinstance(v, list):
            for i in range(len(v)):
                if isinstance(v[i], dict):
                    self.parse_job_order("%s[%s]" % (k, i), v[i], inputs)

                else:
                    break

        return inputs

    def parse_listing(self, listing, inputs):
        for item in listing:

            if "writable" in item:
                raise UnsupportedRequirement(
                    "The TES spec does not allow for writable inputs"
                )

            if "contents" in item:
                loc = self.fs_access.join(self.tmpdir, item["basename"])
                with self.fs_access.open(loc, "wb") as gen:
                    gen.write(item["contents"])
            else:
                loc = item["location"]

            parameter = tes.Input(
                name=item["basename"],
                description="InitialWorkDirRequirement:cwl_input:%s" % (
                    item["basename"]
                ),
                url=file_uri(loc),
                path=self.fs_access.join(
                    self.docker_workdir, item["basename"]
                ),
                type=item["class"].upper()
            )
            inputs.append(parameter)

        return inputs

    def get_inputs(self):
        inputs = []

        # find all primary and secondary input files
        for k, v in self.joborder.items():
            self.parse_job_order(k, v, inputs)

        # manage InitialWorkDirRequirement
        self.parse_listing(self.generatefiles["listing"], inputs)

        return inputs

    def get_envvars(self):
        env = self.environment
        vars_to_preserve = self.runtime_context.preserve_environment
        if self.runtime_context.preserve_entire_environment:
            vars_to_preserve = os.environ
        if vars_to_preserve is not None:
            for key, value in os.environ.items():
                if key in vars_to_preserve and key not in env:
                    # On Windows, subprocess env can't handle unicode.
                    env[key] = str(value) if onWindows() else value
        env["HOME"] = str(self.outdir) if onWindows() else self.outdir
        env["TMPDIR"] = str(self.tmpdir) if onWindows() else self.tmpdir
        return env

    def create_task_msg(self):
        input_parameters = self.get_inputs()
        output_parameters = []

        if self.stdout is not None:
            parameter = tes.Output(
                name="stdout",
                url=self.output2url(self.stdout),
                path=self.output2path(self.stdout)
            )
            output_parameters.append(parameter)

        if self.stderr is not None:
            parameter = tes.Output(
                name="stderr",
                url=self.output2url(self.stderr),
                path=self.output2path(self.stderr)
            )
            output_parameters.append(parameter)

        output_parameters.append(
            tes.Output(
                name="workdir",
                url=self.output2url(""),
                path=self.docker_workdir,
                type="DIRECTORY"
            )
        )

        container = self.get_container()
        cpus = None
        ram = None
        disk = None

        for i in self.builder.requirements:
            if i.get("class", "NA") == "ResourceRequirement":
                cpus = i.get("coresMin", i.get("coresMax", None))
                ram = i.get("ramMin", i.get("ramMax", None))
                disk = i.get("outdirMin", i.get("outdirMax", None))

                if (cpus is None or isinstance(cpus, str)) or \
                   (ram is None or isinstance(ram, str)) or \
                   (disk is None or isinstance(disk, str)):
                    raise UnsupportedRequirement(
                        "cwl-tes does not support dynamic resource requests"
                    )

                ram = ram / 953.674 if ram is not None else None
                disk = disk / 953.674 if disk is not None else None
            elif i.get("class", "NA") == "DockerRequirement":
                if i.get("dockerOutputDirectory", None) is not None:
                    output_parameters.append(
                        tes.Output(
                            name="dockerOutputDirectory",
                            url=self.output2url(""),
                            path=i.get("dockerOutputDirectory"),
                            type="DIRECTORY"
                        )
                    )

        create_body = tes.Task(
            name=self.name,
            description=self.spec.get("doc", ""),
            executors=[
                tes.Executor(
                    command=self.command_line,
                    image=container,
                    workdir=self.docker_workdir,
                    stdout=self.output2path(self.stdout),
                    stderr=self.output2path(self.stderr),
                    stdin=self.stdin,
                    env=self.get_envvars()
                )
            ],
            inputs=input_parameters,
            outputs=output_parameters,
            resources=tes.Resources(
                cpu_cores=cpus,
                ram_gb=ram,
                disk_gb=disk
            ),
            tags={"CWLDocumentId": self.spec.get("id")}
        )

        return create_body

    def run(self, runtimeContext):
        log.debug(
            "[job %s] self.__dict__ in run() ----------------------",
            self.name
        )
        log.debug(pformat(self.__dict__))

        task = self.create_task_msg()

        log.info(
            "[job %s] CREATED TASK MSG----------------------",
            self.name
        )
        log.info(pformat(task))

        try:
            self.id = self.client.create_task(task)
            log.info(
                "[job %s] SUBMITTED TASK ----------------------",
                self.name
            )
            log.info("[job %s] task id: %s ", self.name, self.id)
        except Exception as e:
            log.error(
                "[job %s] Failed to submit task to TES service:\n%s",
                self.name, e
            )
            raise WorkflowException(e)

        max_tries = 10
        current_try = 1
        while not self.is_done():
            delay = 1.5 * current_try**2
            time.sleep(
                random.randint(
                    round(
                        delay -
                        0.5 *
                        delay),
                    round(
                        delay +
                        0.5 *
                        delay)))
            log.debug(
                "[job %s] POLLING %s", self.name, pformat(self.id)
            )
            try:
                task = self.client.get_task(self.id, "MINIMAL")
                self.state = task.state
            except Exception as e:
                log.error("[job %s] POLLING ERROR %s", self.name, e)
                if current_try <= max_tries:
                    current_try += 1
                    continue
                else:
                    log.error("[job %s] MAX POLLING RETRIES EXCEEDED",
                              self.name)
                    break

        try:
            outputs = self.collect_outputs(self.outdir)
            cleaned_outputs = {}
            for k, v in outputs.items():
                if isinstance(k, bytes):
                    k = k.decode("utf8")
                if isinstance(v, bytes):
                    v = v.decode("utf8")
                cleaned_outputs[k] = v
                self.outputs = cleaned_outputs
            self.output_callback(self.outputs, "success")
        except WorkflowException as e:
            log.error("[job %s] job error:\n%s", self.name, e)
            self.output_callback({}, "permanentFail")
        except Exception as e:
            log.error("[job %s] job error:\n%s", self.name, e)
            self.output_callback({}, "permanentFail")
        finally:
            if self.outputs is not None:
                log.info(
                    "[job %s] OUTPUTS ------------------",
                    self.name
                )
                log.info(pformat(self.outputs))
            self.cleanup(self.runtime_context.rm_tmpdir)
        return

    def is_done(self):
        terminal_states = ["COMPLETE", "CANCELED", "EXECUTOR_ERROR",
                           "SYSTEM_ERROR"]
        if self.state in terminal_states:
            log.info(
                "[job %s] FINAL JOB STATE: %s ------------------",
                self.name, self.state
            )
            if self.state != "COMPLETE":
                log.error(
                    "[job %s] task id: %s", self.name, self.id
                )
                log.error(
                    "[job %s] logs: %s",
                    self.name, self.client.get_task(self.id, "FULL").logs
                )
            return True
        return False

    def cleanup(self, rm_tmpdir):
        log.debug(
            "[job %s] STARTING CLEAN UP ------------------",
            self.name
        )
        if self.stagedir and os.path.exists(self.stagedir):
            log.debug(
                "[job %s] Removing input staging directory %s",
                self.name, self.stagedir
            )
            shutil.rmtree(self.stagedir, True)

        if rm_tmpdir:
            log.debug(
                "[job %s] Removing temporary directory %s",
                self.name, self.tmpdir
            )
            shutil.rmtree(self.tmpdir, True)

    def output2url(self, path):
        if path is not None:
            return file_uri(
                self.fs_access.join(self.outdir, os.path.basename(path))
            )
        return None

    def output2path(self, path):
        if path is not None:
            return self.fs_access.join(self.docker_workdir, path)
        return None
