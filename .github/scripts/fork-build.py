#!/usr/bin/env python3
"""Keep fork fixes local and prepare tag-based, customized build snapshots."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


KEYS = {
    "OPTION_CUSTOM_RENDEZVOUS_SERVER": "custom-rendezvous-server",
    "OPTION_API_SERVER": "api-server",
    "OPTION_KEY": "key",
    "OPTION_HIDE_SERVER_SETTINGS": "hide-server-settings",
    "OPTION_ALLOW_DEEP_LINK_SERVER_SETTINGS": "allow-deep-link-server-settings",
}


def git(repo, *args, input=None, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=input, text=True,
        stdout=subprocess.PIPE, check=check,
    ).stdout.strip()


def repair(root):
    workflow = root / ".github/workflows/flutter-build.yml"
    source = workflow.read_text()
    match = re.search(r"(?ms)^  generate-sbom:\n.*?(?=^  [\w-]+:|\Z)", source)
    if match is None:
        raise RuntimeError("generate-sbom job not found; review upstream workflow changes")
    job = match.group()
    permissions = "    permissions:\n      contents: write\n\n"
    if "    permissions:" in job and job.count(permissions) != 1:
        raise RuntimeError("Unexpected SBOM permissions; review upstream workflow changes")
    repaired = source[:match.start()] + job.replace(permissions, "") + source[match.end():]
    if repaired != source:
        workflow.write_text(repaired)

    keys = root / "libs/base/src/config/keys.rs"
    if keys.exists():
        source = keys.read_text()
        for name, value in KEYS.items():
            declaration = f'pub const {name}: &str = "{value}";\n'
            if re.search(rf"pub const {name}\s*:", source) and source.count(declaration) != 1:
                raise RuntimeError(f"Unexpected definition for {name}")
            source = source.replace(declaration, "")
        if source != keys.read_text():
            keys.write_text(source)


def clone(source, destination, ref):
    subprocess.run(
        ["git", "clone", "--shared", "--no-checkout", str(source), str(destination)],
        check=True, stdout=subprocess.DEVNULL,
    )
    git(destination, "remote", "set-url", "origin", git(source, "remote", "get-url", "origin"))
    header = subprocess.run(
        ["git", "-C", str(source), "config", "--get", "http.https://github.com/.extraheader"],
        text=True, stdout=subprocess.PIPE,
    )
    if header.returncode == 0:
        git(destination, "config", "http.https://github.com/.extraheader", header.stdout.strip())
    elif header.returncode != 1:
        raise RuntimeError("Unable to read checkout credentials")
    git(destination, "checkout", "--detach", ref)
    for key, fallback in [("user.name", "github-actions[bot]"),
                          ("user.email", "41898282+github-actions[bot]@users.noreply.github.com")]:
        result = subprocess.run(["git", "-C", str(source), "config", "--get", key],
                                text=True, stdout=subprocess.PIPE)
        if result.returncode not in (0, 1):
            raise RuntimeError(f"Unable to read {key}")
        git(destination, "config", key, result.stdout.strip() or fallback)


def publish(repo, ref, no_push):
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    if not no_push:
        existing = git(repo, "ls-remote", "origin", f"refs/heads/{ref}")
        if existing:
            sha = existing.split()[0]
            git(repo, "fetch", "origin", sha)
            if git(repo, "rev-parse", f"{sha}^{{tree}}") != tree:
                raise RuntimeError(f"Existing snapshot {ref} has different contents")
            return sha
        git(repo, "push", "origin", f"HEAD:refs/heads/{ref}")
    return git(repo, "rev-parse", "HEAD")


def commit(repo, message):
    timestamp = git(repo, "show", "-s", "--format=%cI", "HEAD")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message], check=True,
        stdout=subprocess.DEVNULL,
        env=dict(os.environ, GIT_AUTHOR_DATE=timestamp, GIT_COMMITTER_DATE=timestamp),
    )


def prepare(root, hbb, tag, no_push):
    if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-\d+)?", tag):
        raise RuntimeError("Expected an upstream release tag, such as 1.5.0")
    tag_sha = git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    policy_sha = git(hbb, "rev-parse", "HEAD")
    policy_base = git(hbb, "merge-base", "HEAD", "upstream/main")
    policy = git(hbb, "diff", "--binary", policy_base, policy_sha, "--", "build.rs", "src/config.rs")
    if not policy:
        raise RuntimeError("Fork server policy is missing")
    wrapper = (root / ".github/workflows/flutter-tag.yml").read_bytes()
    identity = hashlib.sha256(policy.encode() + wrapper + Path(__file__).read_bytes()).hexdigest()[:12]
    ref = f"codex/release/{tag}/{tag_sha[:12]}-{policy_sha[:12]}-{identity}"
    upstream_hbb = git(root, "ls-tree", tag_sha, "libs/hbb_common").split()[2]

    with tempfile.TemporaryDirectory(prefix="rustdesk-release-") as temporary:
        hbb_snapshot = Path(temporary) / "hbb_common"
        clone(hbb, hbb_snapshot, upstream_hbb)
        git(hbb_snapshot, "apply", "--index", input=policy + "\n")
        git(hbb_snapshot, "diff", "--cached", "--check")
        commit(hbb_snapshot, f"Apply fork server policy to upstream {tag}")
        hbb_sha = publish(hbb_snapshot, ref, no_push)

        snapshot = Path(temporary) / "rustdesk"
        clone(root, snapshot, tag_sha)
        (snapshot / ".github/workflows/flutter-tag.yml").write_bytes(wrapper)
        gitmodules = snapshot / ".gitmodules"
        hbb_url = git(hbb, "remote", "get-url", "origin")
        git(snapshot, "config", "--file", str(gitmodules), "submodule.libs/hbb_common.url", hbb_url)
        repair(snapshot)
        git(snapshot, "add", ".gitmodules", ".github/workflows/flutter-tag.yml",
            ".github/workflows/flutter-build.yml")
        if (snapshot / "libs/base/src/config/keys.rs").exists():
            git(snapshot, "add", "libs/base/src/config/keys.rs")
        git(snapshot, "update-index", "--cacheinfo", f"160000,{hbb_sha},libs/hbb_common")
        git(snapshot, "diff", "--cached", "--check")
        commit(snapshot, f"Build upstream {tag} with fork server policy")
        publish(snapshot, ref, no_push)
        print(f"upstream={tag_sha} hbb_upstream={upstream_hbb} policy={policy_sha} "
              f"tree={git(snapshot, 'rev-parse', 'HEAD^{tree}')}", file=sys.stderr)
    print(ref)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["repair", "prepare-release"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--hbb", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()
    if args.command == "repair":
        repair(args.root)
    else:
        if args.hbb is None or args.tag is None:
            parser.error("prepare-release requires --hbb and --tag")
        prepare(args.root.resolve(), args.hbb.resolve(), args.tag, args.no_push)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
