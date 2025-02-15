"""Autoupdate older conda dependencies in the requirements section."""

import collections
import itertools
import re
import xml.etree.ElementTree as ET

import packaging.version
import requests
import yaml
from bioblend import toolshed
from galaxy.tool_util.deps import conda_util

import planemo.conda
from planemo.io import (
    error,
    info,
)

AUTOUPDATE_TOOLSHED_URL = "https://toolshed.g2.bx.psu.edu"


def find_macros(xml_tree):
    """
    Get macros from the XML tree
    """
    macros = []
    for macro_import in xml_tree.iter("import"):
        macros.append(macro_import.text)
    return macros


def get_requirements(xml_tree):
    """
    Get requirements from the XML tree
    """
    requirements = {}
    main_req = None
    for requirement in xml_tree.iter("requirement"):
        if requirement.attrib.get("version") == "@TOOL_VERSION@":
            main_req = requirement.text
        else:
            requirements[requirement.text] = {
                "tag": ET.tostring(requirement, encoding="unicode").strip(),
                "text": requirement.attrib.get("version"),
            }
    return requirements, main_req


def get_tokens(xml_tree):
    """
    Get tokens from the XML tree
    """
    tokens = {}
    for token in xml_tree.iter("token"):
        tokens[token.attrib["name"]] = {"tag": ET.tostring(token, encoding="unicode").strip(), "text": token.text}
    return tokens


def check_conda(tool_name, ctx, **kwds):
    """
    Get the most up-to-date conda version for a tool requirement
    """
    conda_context = planemo.conda.build_conda_context(ctx, **kwds)
    if not conda_context.is_conda_installed():
        # check directly via Anaconda API
        r = requests.get("https://api.anaconda.org/search", params={"name": tool_name})
        search_results = itertools.chain.from_iterable(
            n["versions"] for n in r.json() if n["name"] == tool_name and n["owner"] in kwds["conda_ensure_channels"]
        )
        return sorted(search_results, key=packaging.version.parse, reverse=True)[0]

    target = planemo.conda.conda_util.CondaTarget(tool_name)
    search_results = conda_util.best_search_result(target, conda_context=conda_context)
    return search_results[0]["version"]


def update_xml(tool_path, xml_tree, tags_to_update, wrapper_version_token, is_macro=False):
    """
    Write modified XML to tool_path
    """

    def update_token(xml_text, tag, token_value):
        new_tag = f">{token_value}<".join(re.split(">.*<", tag))
        return re.sub(tag, new_tag, xml_text)

    def update_requirement(xml_text, tag, requirement_value):
        new_tag = f'version="{requirement_value}"'.join(re.split('version=".*"', tag))
        return re.sub(tag, new_tag, xml_text)

    with open(tool_path, "r+", newline="") as f:
        xml_text = f.read()
        for tag_to_update in tags_to_update:
            if tag_to_update["type"] == "token":
                xml_text = update_token(xml_text, tag_to_update["tag"], tag_to_update["value"])
            if tag_to_update["type"] == "requirement":
                xml_text = update_requirement(xml_text, tag_to_update["tag"], tag_to_update["value"])
        if wrapper_version_token == 0 and not is_macro:
            # i.e. @VERSION_SUFFIX@ not specified so update the version directly in the tool tag
            tool_tag = re.sub(
                'version="@TOOL_VERSION@.*?"',
                'version="@TOOL_VERSION@+galaxy0"',
                re.findall('<tool .*version="@TOOL_VERSION@.*">', xml_text)[0],
            )
            xml_text = re.sub('<tool .*version="@TOOL_VERSION@.*">', tool_tag, xml_text)
        f.seek(0)
        f.truncate()
        f.write(xml_text)


def create_requirement_dict(xml_files, skip_reqs):
    """
    Create dict with requirements and find main requirement
    """
    requirements = {}
    main_req = None
    for k, v in xml_files.items():
        file_reqs, file_main_req = get_requirements(v)
        requirements[k] = {k: v for k, v in file_reqs.items() if k not in skip_reqs}
        if file_main_req:
            if main_req:
                error("Multiple requirements use the token @TOOL_VERSION@!")
            main_req = (file_main_req, k)
    if not main_req:
        error("No requirement uses the token @TOOL_VERSION@!")
    return requirements, main_req


def create_token_dict(ctx, xml_files, main_req, **kwds):
    """
    Create dict with relevant tokens and check conda requirements for main
    """
    tokens = {}
    current_main_req, updated_main_req = None, None
    xml_to_update = collections.defaultdict(list)
    for k, v in xml_files.items():
        tokens[k] = get_tokens(v)
        # check if it is @TOOL_VERSION@ and if so do check_conda
        if "@TOOL_VERSION@" in tokens[k]:
            current_main_req = tokens[k]["@TOOL_VERSION@"]["text"]
            updated_main_req = check_conda(main_req[0], ctx, **kwds)
            if current_main_req:
                xml_to_update[k].append(
                    {"type": "token", "tag": tokens[k]["@TOOL_VERSION@"]["tag"], "value": updated_main_req}
                )

    return tokens, xml_to_update, current_main_req, updated_main_req


def perform_required_update(
    ctx, xml_files, tool_path, requirements, tokens, xml_to_update, wrapper_version_token, **kwds
):
    """
    Carry out the update, if requirements are out-of-date
    """
    # check all requirements
    for k, v in requirements.items():
        for req in v:
            req_check = check_conda(req, ctx, **kwds)
            # print(req_check, v[req]['text'])
            if req_check != v[req]["text"]:
                xml_to_update[k].append({"type": "requirement", "tag": v[req]["tag"], "value": req_check})

    # check all tokens, if wrapper_version_token exists
    if wrapper_version_token:
        for k, v in tokens.items():
            if wrapper_version_token in v:
                xml_to_update[k].append({"type": "token", "tag": v[wrapper_version_token]["tag"], "value": 0})

    # finally, update each file separately
    for k, v in xml_files.items():
        update_xml(k, v, xml_to_update[k], wrapper_version_token, is_macro=(k != tool_path))
    info(f"Tool {tool_path} successfully updated.")
    return set(xml_files)


def autoupdate_tool(ctx, tool_path, modified_files, **kwds):
    """
    Autoupdate an XML file
    """
    modified_files = modified_files or set()
    # create a dict of all files that need editing - wrapper plus macros
    xml_files = {tool_path: ET.parse(tool_path)}

    # get name of token which defines the wrapper version; if just an integer, None
    versions = xml_files[tool_path].getroot().attrib.get("version")
    if versions:
        versions = versions.split("+galaxy")
        if versions[0] != "@TOOL_VERSION@":
            error("Tool version does not contain @TOOL_VERSION@ as required by autoupdate.")
            return
        elif len(versions) == 1:
            wrapper_version_token = None
        else:
            if versions[1][0] == versions[1][-1] == "@":
                wrapper_version_token = versions[1]
            else:
                wrapper_version_token = 0  # assume an int
    else:
        wrapper_version_token = None

    # add macros to xml_files
    for macro in find_macros(xml_files[tool_path]):
        macro_path = "/".join(tool_path.split("/")[:-1] + [macro])
        xml_files[macro_path] = ET.parse(macro_path)

    requirements, main_req = create_requirement_dict(xml_files, kwds.get("skip_requirements", "").split(","))
    tokens, xml_to_update, current_main_req, updated_main_req = create_token_dict(ctx, xml_files, main_req, **kwds)

    if current_main_req == updated_main_req and not (modified_files & set(xml_files)):
        info(f"No updates required or made to {tool_path}.")
        return  # end here if no update needed

    if kwds.get("dry_run"):
        error(
            f"Update required to {tool_path}! Tool main requirement has version {current_main_req}, newest conda version is {updated_main_req}"
        )
        return

    else:
        info(f"Updating {tool_path.split('/')[-1]} from version {current_main_req} to {updated_main_req}")
        return perform_required_update(
            ctx, xml_files, tool_path, requirements, tokens, xml_to_update, wrapper_version_token, **kwds
        )


def _update_wf(config, workflow_id, instance=False):
    """
    Recursively update a workflow, including subworkflows
    """
    wf = config.user_gi.make_get_request(
        f"{config.user_gi.url}/workflows/{workflow_id}", params={"instance": instance}
    ).json()
    for step in wf.get("steps", {}).values():
        if step["type"] == "subworkflow":
            # update subworkflows before the main workflow
            _update_wf(config, step["workflow_id"], instance=True)
    config.user_gi.workflows.refactor_workflow(wf["id"], actions=[{"action_type": "upgrade_all_steps"}])


def outdated_tools(ctx, wf_dict, ts):
    def check_tool_step(step, ts):  # return a dict with current and newest tool version, in case they don't match
        if not step["tool_id"].startswith(AUTOUPDATE_TOOLSHED_URL[8:]):
            return {}  # assume a built in tool
        try:
            repos = ts.repositories._get(params={"tool_ids": step["tool_id"]})
        except Exception:
            ctx.log(f"The ToolShed returned an error when searching for the most recent version of {step['tool_id']}")
            return {}
        base_id = "/".join(step["tool_id"].split("/")[:-1])
        tool_ids_found = {
            tool["guid"] for repo in repos.values() if type(repo) == dict for tool in repo.get("tools", [])
        }
        updated_tool_id = sorted(
            {tool_id for tool_id in tool_ids_found if f"{base_id}/" in tool_id},
            key=lambda n: packaging.version.parse(n),
        )[-1]
        if step["tool_id"] != updated_tool_id:
            return {base_id: {"current": step["tool_id"], "updated": updated_tool_id}}
        else:
            return {}

    outdated_tool_dict = {}
    steps = wf_dict["steps"].values() if type(wf_dict["steps"]) == dict else wf_dict["steps"]
    for step in steps:
        if step.get("type", "tool") == "tool" and not step.get("run", {}).get("class") == "GalaxyWorkflow":
            outdated_tool_dict.update(check_tool_step(step, ts))
        elif step.get("type") == "subworkflow":  # GA SWF
            outdated_tool_dict.update(outdated_tools(ctx, step["subworkflow"], ts))
        elif step.get("run", {}).get("class") == "GalaxyWorkflow":  # gxformat2 SWF
            outdated_tool_dict.update(outdated_tools(ctx, step["run"], ts))
        else:
            continue
    return outdated_tool_dict


def get_tools_to_update(ctx, workflow, tools_to_skip):
    # before we run the autoupdate, we check the tools against the toolshed to see if there
    # are any new versions. This saves spinning up Galaxy and installing the tools if there
    # is nothing to do, and also allows us to collect a list of the tools which need updating
    with open(workflow.path) as f:
        wf_dict = yaml.load(f, Loader=yaml.SafeLoader)

    ts = toolshed.ToolShedInstance(url=AUTOUPDATE_TOOLSHED_URL)
    tools_to_update = outdated_tools(ctx, wf_dict, ts)
    return {tool: versions for tool, versions in tools_to_update.items() if tool not in tools_to_skip}


def autoupdate_wf(ctx, config, wf):
    workflow_id = config.workflow_id_for_runnable(wf)
    _update_wf(config, workflow_id)
    return config.user_gi.workflows.export_workflow_dict(workflow_id)


def fix_workflow_ga(original_wf, updated_wf):
    # the Galaxy refactor action can't do everything right now... some manual fixes here
    # * bump release number if present
    # * order steps numerically, leave everything else sorted as in the original workflow
    # * recurse over subworkflows
    edited_wf = original_wf.copy()
    updated_wf_steps = collections.OrderedDict(sorted(updated_wf["steps"].items(), key=lambda item: int(item[0])))
    edited_wf["steps"] = updated_wf_steps
    # check release; bump if it exists
    if edited_wf.get("release"):
        release = [int(n) for n in edited_wf["release"].split(".")]
        release[-1] += 1
        edited_wf["release"] = ".".join([str(n) for n in release])
    # iterate over the steps
    for step in edited_wf["steps"]:
        # recurse over subworkflows
        if edited_wf["steps"][step].get("type") == "subworkflow":
            edited_wf["steps"][step]["subworkflow"] = fix_workflow_ga(
                edited_wf["steps"][step]["subworkflow"], updated_wf["steps"][step]["subworkflow"]
            )
    return edited_wf


def fix_workflow_gxformat2(original_wf, updated_wf):
    # does the same as fix_workflow_ga for gxformat2
    edited_wf = original_wf.copy()
    # check release; bump if it exists
    if edited_wf.get("release"):
        release = [int(n) for n in edited_wf["release"].split(".")]
        release[-1] += 1
        edited_wf["release"] = ".".join([str(n) for n in release])
    # iterate over the steps
    for step_index, step in enumerate(edited_wf["steps"]):
        # recurse over subworkflows
        if step.get("run", {}).get("class") == "GalaxyWorkflow":  # subworkflow
            step["run"] = fix_workflow_gxformat2(
                step["run"], updated_wf["steps"][str(step_index + len(original_wf["inputs"]))]["subworkflow"]
            )
        # fix tool_id and content_id to march tool_version
        elif updated_wf["steps"][str(step_index + len(original_wf["inputs"]))]["type"] == "tool":
            if (
                updated_wf["steps"][str(step_index + len(original_wf["inputs"]))]
                .get("tool_id", "")
                .startswith(AUTOUPDATE_TOOLSHED_URL[8:])
            ):
                step["tool_version"] = updated_wf["steps"][str(step_index + len(original_wf["inputs"]))]["tool_version"]
                step["tool_id"] = updated_wf["steps"][str(step_index + len(original_wf["inputs"]))]["tool_id"]
                step["content_id"] = updated_wf["steps"][str(step_index + len(original_wf["inputs"]))]["content_id"]

    return edited_wf
