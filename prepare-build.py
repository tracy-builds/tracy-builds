#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "requests",
# ]
# ///

"""
Prepare Tracy build by creating branch and generating combined workflow.

Usage:
    uv run prepare-build.py <tracy-tag> [--no-push]

Examples:
    python prepare-build.py v0.12.2
    python prepare-build.py v0.11.0 --no-push
"""

import sys
import subprocess
import yaml
import requests
from pathlib import Path
import time
import argparse
from copy import deepcopy


def str_presenter(dumper, data):
    if data.count("\n") > 0:
        # Remove any trailing spaces messing out the output.
        block = "\n".join([line.rstrip() for line in data.splitlines()])
        if data.endswith("\n"):
            block += "\n"
        return dumper.represent_scalar("tag:yaml.org,2002:str", block, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, str_presenter)
yaml.representer.SafeRepresenter.add_representer(str, str_presenter)


def run_command(cmd, check=True, capture=False):
    print(f"Running: {' '.join(cmd)}")
    if capture:
        result = subprocess.run(cmd, capture_output=True, text=True, check=check)
        return result.stdout.strip()
    else:
        result = subprocess.run(cmd, check=check)
        return result.returncode == 0


def fetch_tracy_workflows(tag):
    """Fetch Tracy workflow files from GitHub."""
    print(f"\n=== Fetching Tracy workflows for {tag} ===")

    base_url = (
        f"https://raw.githubusercontent.com/wolfpld/tracy/{tag}/.github/workflows"
    )
    workflows_dir = Path("tracy-workflows")
    workflows_dir.mkdir(exist_ok=True)

    workflows = {
        "build.yml": f"{base_url}/build.yml",
        "linux.yml": f"{base_url}/linux.yml",
    }

    fetched = {}
    for name, url in workflows.items():
        print(f"Fetching {name}...")
        response = requests.get(url)

        if response.status_code == 200:
            workflow_path = workflows_dir / name
            workflow_path.write_text(response.text)
            print(f"  ✓ Saved to {workflow_path}")
            fetched[name] = workflow_path
        else:
            print(f"  ✗ Failed: HTTP {response.status_code}")
            if response.status_code == 404:
                print(f"    (Workflow may not exist for tag {tag})")

    return fetched


def modify_job(job_config, tracy_tag, job_name):
    """Modify jobs."""
    if "steps" not in job_config:
        return

    if job_name == "alpine":
        del job_config["container"]
        steps = [
            {
                "uses": "jirutka/setup-alpine@v1",
                "with": {
                    "packages": "build-base freetype wayland wayland-dev dbus dbus-dev libxkbcommon mesa-egl glfw meson cmake git wayland-protocols nodejs"
                }
            }
        ]
    else:
        steps = []
    for step in job_config["steps"]:
        if "uses" in step:
            # point checkout at tracy repo and tag
            if "checkout" in step["uses"]:
                step["with"] = {
                    "repository": "wolfpld/tracy",
                    "ref": f"${{{{ github.event.inputs.tracy_tag || '{tracy_tag}' }}}}",
                }

            # add build flags matrix to upload
            if "actions/upload-artifact" in step["uses"]:
                if job_name == "build":
                    step["with"]["name"] = (
                        "${{ matrix.os }}${{ matrix.build_flags.postfix }}"
                    )
                else:
                    step["with"]["name"] = ("arch" if job_name == "linux" else "alpine") +"-linux${{ matrix.build_flags.postfix }}"

        if "run" in step:
            if job_name == "alpine":
                step["shell"] = "alpine.sh {0}"
            # tracy uses ${{ github.sha }} to pass git ref to cmake
            # luckily, cmake calls "git log <ref>" so we can just pass the tag
            if "${{ github.sha }}" in step["run"]:
                step["run"] = step["run"].replace("${{ github.sha }}", f'"{tracy_tag}"')

            # add glfw dependency for -DLEGACY=1 builds
            if "pacman" in step["run"]:
                if job_name == "linux":
                    step["run"] = step["run"].replace("cmake", "cmake glfw")
                if job_name == "alpine":
                    continue

            # inject matrix args into meson and cmake
            if "meson" in step["run"]:
                step["run"] = step["run"].replace(
                    "meson setup -D", "meson setup ${{ matrix.build_flags.meson }} -D"
                )
            if "cmake -B" in step["run"]:
                step["run"] = step["run"].replace(
                    " -DCMAKE_BUILD_TYPE=Release",
                    " ${{ matrix.build_flags.cmake }} -DCMAKE_BUILD_TYPE=Release",
                )

        # remove Test builds
        if (
            "name" in step
            and "Test" in step["name"]
            and "run" in step
            and "test/build" in step["run"]
        ):
            continue
        steps.append(step)

    # use new, cleaned, steps
    job_config["steps"] = steps

    # add build_flags matrix
    if "strategy" not in job_config:
        job_config["strategy"] = {}

    if "matrix" not in job_config["strategy"]:
        job_config["strategy"]["matrix"] = {}

    job_config["strategy"]["matrix"]["build_flags"] = [
        {"cmake": "-DTRACY_LTO=ON", "meson": "", "postfix": ""},
        # TODO: likely broken
        # {
        #     "cmake": "-DTRACY_ON_DEMAND=ON -DTRACY_LTO=ON",
        #     "meson": "-Dtracy:on_demand=true",
        #     "postfix": "-ondemand",
        # },
    ]

    if job_name in ["linux", "alpine"]:
        job_config["strategy"]["matrix"]["build_flags"].append(
            {
                "cmake": "-DLEGACY=ON -DTRACY_LTO=ON",
                "meson": "",
                "postfix": "-x11",
            }
        )


def generate_combined_workflow(workflows, tracy_tag):
    """Generate combined workflow from Tracy's workflows."""
    print("\n=== Generating combined workflow ===")

    combined = {
        "name": "Combined Tracy Build",
        "on": {"push": {"tags": ["v*"]}},
        "permissions": {"contents": "write"},
        "jobs": {},
    }

    # Process build.yml (Windows/macOS)
    if "build.yml" in workflows and False: # testing
        print("Processing build.yml...")
        with open(workflows["build.yml"], "r") as f:
            build_wf = yaml.safe_load(f)

        if "jobs" in build_wf:
            for job_name, job_config in build_wf["jobs"].items():
                print(f"  Adding job: tracy-{job_name}")
                modify_job(job_config, tracy_tag, "build")
                combined["jobs"][f"tracy-{job_name}"] = job_config
                if "env" in build_wf:
                    if "env" not in combined["jobs"][f"tracy-{job_name}"]:
                        combined["jobs"][f"tracy-{job_name}"]["env"] = {}
                    combined["jobs"][f"tracy-{job_name}"]["env"].update(build_wf["env"])

    # Process linux.yml
    if "linux.yml" in workflows:
        print("Processing linux.yml...")
        with open(workflows["linux.yml"], "r") as f:
            linux_wf = yaml.safe_load(f)

        if "jobs" in linux_wf:
            for job_name, job_config in linux_wf["jobs"].items():
                # job_config_arch = deepcopy(job_config)
                # print(f"  Adding job: tracy-linux-{job_name}")
                # modify_job(job_config_arch, tracy_tag, "linux")
                # combined["jobs"][f"tracy-linux-{job_name}"] = job_config_arch
                # if "env" in linux_wf:
                #     if "env" not in combined["jobs"][f"tracy-linux-{job_name}"]:
                #         combined["jobs"][f"tracy-linux-{job_name}"]["env"] = {}
                #     combined["jobs"][f"tracy-linux-{job_name}"]["env"].update(
                #         linux_wf["env"]
                #     )
                job_config_alpine = deepcopy(job_config)
                job_name = "build-alpine"
                print(f"  Adding job: tracy-linux-{job_name}")
                modify_job(job_config_alpine, tracy_tag, "alpine")
                combined["jobs"][f"tracy-linux-{job_name}"] = job_config_alpine
                if "env" in linux_wf:
                    if "env" not in combined["jobs"][f"tracy-linux-{job_name}"]:
                        combined["jobs"][f"tracy-linux-{job_name}"]["env"] = {}
                    combined["jobs"][f"tracy-linux-{job_name}"]["env"].update(
                        linux_wf["env"]
                    )


    # Add release job
    print("Adding release job...")
    all_job_names = list(combined["jobs"].keys())

    with open("./create_release.yml", "r") as f:
        release = yaml.safe_load(f)["create-release"]
    release["needs"] = all_job_names
    combined["jobs"]["create-release"] = release

    # Write combined workflow
    workflow_dir = Path(".github/workflows")
    workflow_dir.mkdir(parents=True, exist_ok=True)

    output_path = workflow_dir / "build-combined.yml"
    with open(output_path, "w") as f:
        yaml.dump(combined, f, default_flow_style=False, sort_keys=False)

    print(f"  ✓ Written to {output_path}")

    return output_path


def commit_and_push(branch, tracy_tag, push=True):
    """Commit changes and optionally push."""
    print("\n=== Committing changes ===")

    # Configure git if needed
    try:
        run_command(["git", "config", "user.name"], capture=True)
    except subprocess.CalledProcessError:
        run_command(["git", "config", "user.name", "github-actions[bot]"])
        run_command(
            [
                "git",
                "config",
                "user.email",
                "github-actions[bot]@users.noreply.github.com",
            ]
        )

    # Add files
    run_command(["git", "add", ".github/workflows/build-combined.yml"])
    run_command(["git", "add", "tracy-workflows/"])

    # Commit
    commit_msg = f"Add combined workflow for {tracy_tag}"
    run_command(
        [
            "git",
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=github-actions[bot]@users.noreply.github.com",
            "commit",
            "-m",
            commit_msg,
        ]
    )

    if push:
        existing_tag = run_command(
            ["git", "tag", "-l", tracy_tag], capture=True, check=False
        )
        if existing_tag:
            print(f"Local tag {tracy_tag} exists, deleting...")
            run_command(["git", "tag", "-d", tracy_tag])
        remote_tag = run_command(
            ["git", "ls-remote", "--tags", "origin", tracy_tag],
            capture=True,
            check=False,
        )
        if remote_tag:
            print(f"Remote tag {tracy_tag} exists, deleting...")
            run_command(["git", "push", "origin", "--delete", tracy_tag], check=False)

        print("\n=== Pushing to remote ===")
        run_command(["git", "push", "origin", branch])
        time.sleep(1)

        print(f"\n=== Tagging commit as {tracy_tag} ===")
        run_command(["git", "tag", tracy_tag])
        run_command(["git", "push", "origin", tracy_tag])
    else:
        print("\n=== Skipping push (--no-push specified) ===")
        print(f"To push manually: git push origin {branch}")


def main():
    parser = argparse.ArgumentParser(
        description="Prepare Tracy build branch and workflow"
    )
    parser.add_argument("tracy_tag", help="Tracy tag to build (e.g., v0.12.2)")
    parser.add_argument(
        "--no-push", action="store_true", help="Do not push to remote (for testing)"
    )
    parser.add_argument(
        "--remote", default="origin", help="Git remote name (default: origin)"
    )

    args = parser.parse_args()

    tracy_tag = args.tracy_tag
    remote = args.remote
    should_push = not args.no_push

    print(f"{'=' * 60}")
    print(f"Preparing build for Tracy {tracy_tag}")
    print(f"{'=' * 60}")

    try:
        run_command(["git", "checkout", "main"], check=False)

        # Step 1: Create build branch
        branch = f"build-{tracy_tag}"

        print(f"\n=== Creating branch: {branch} ===")
        if should_push:
            # Fetch to see if branch exists remotely
            run_command(["git", "fetch", remote], check=False)

            # Check if remote branch exists
            remote_branches = run_command(
                ["git", "ls-remote", "--heads", remote, branch], capture=True
            )

            if remote_branches:
                print(f"Remote branch {branch} exists, deleting...")
                run_command(["git", "push", remote, "--delete", branch], check=False)

        # Check if local branch exists
        local_branches = run_command(["git", "branch", "--list", branch], capture=True)
        if local_branches:
            print(f"Local branch {branch} exists, deleting...")
            run_command(["git", "branch", "-D", branch], check=False)

        # Create new branch
        run_command(["git", "checkout", "-b", branch])

        # Step 2: Fetch Tracy workflows
        workflows = fetch_tracy_workflows(tracy_tag)

        if not workflows:
            print("\n✗ Failed to fetch any workflows")
            return 1

        # Step 3: Generate combined workflow
        combined_path = generate_combined_workflow(workflows, tracy_tag)

        # Step 4: Commit and push
        commit_and_push(branch, tracy_tag, push=should_push)

        print(f"\n{'=' * 60}")
        print("✓ SUCCESS")
        print(f"{'=' * 60}")
        print(f"Branch: {branch}")
        print(f"Combined workflow: {combined_path}")

        return 0

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
