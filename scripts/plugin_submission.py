#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any


MAX_DOWNLOAD_SIZE = 100 << 20
MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_FILE_SIZE = 128 << 20
MAX_EXTRACTED_SIZE = 512 << 20
MAX_MANIFEST_SIZE = 1 << 20
MAX_JSON_SIZE = 4 << 20
MAX_REDIRECTS = 10

GITHUB_REPOSITORY_FIELD = "GitHub 仓库地址 / GitHub repository URL"
PROJECT_URL_FIELD = "项目地址 / Project URL"
DOWNLOAD_FIELD = "插件包下载地址 / Plugin package URL"
NAME_FIELD = "插件名称 / Plugin name"
SHORT_FIELD = "插件唯一短名称 / Unique plugin short name"
VERSION_FIELD = "插件版本 / Plugin version"
DESCRIPTION_FIELD = "插件描述 / Plugin description"
AUTHOR_FIELD = "作者 / Author"
GITHUB_CONFIRMATION_FIELD = "我已确保 / I confirm"
EXTERNAL_CONFIRMATION_FIELD = "我已确认 / I confirm"
PUBLIC_RELEASE_CONFIRMATION = "仓库必须公开，且最新 Release 中必须提供可下载的插件包。"
NO_MALICIOUS_CODE_CONFIRMATION = "插件包中没有恶意代码。"
MANUAL_UPDATE_CONFIRMATION = "非 GitHub 托管的插件不会由 Actions 自动更新"

SHORT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
FORM_FIELD_PATTERN = re.compile(
    r"^### (?P<label>[^\r\n]+)\r?\n+(?P<value>.*?)(?=\r?\n### |\Z)",
    re.MULTILINE | re.DOTALL,
)
GITHUB_HOSTS = {
    "github.com",
    "www.github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "githubusercontent.com",
}


class SubmissionError(Exception):
    pass


class TransientError(Exception):
    pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPClient:
    def __init__(self, github_token: str | None = None):
        self.github_token = github_token
        self.opener = urllib.request.build_opener(NoRedirectHandler())

    def get_github_json(self, path: str) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "komari-plugin-market-submission-checker",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        data = self._request(
            f"https://api.github.com{path}",
            MAX_JSON_SIZE,
            headers=headers,
            require_public=False,
        )
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SubmissionError(
                f"GitHub API 返回了无效 JSON / GitHub API returned invalid JSON: {error}"
            ) from error
        if not isinstance(value, dict):
            raise SubmissionError("GitHub API 返回格式无效 / GitHub API returned an invalid response")
        return value

    def download(self, url: str) -> bytes:
        return self._request(
            url,
            MAX_DOWNLOAD_SIZE,
            headers={"User-Agent": "komari-plugin-market-submission-checker"},
            require_public=True,
        )

    def _request(
        self,
        url: str,
        max_size: int,
        *,
        headers: dict[str, str],
        require_public: bool,
    ) -> bytes:
        current = url
        for redirect_count in range(MAX_REDIRECTS + 1):
            if require_public:
                validate_public_url(current)
            request = urllib.request.Request(current, headers=headers)
            try:
                response = self.opener.open(request, timeout=45)
            except urllib.error.HTTPError as error:
                if error.code in {301, 302, 303, 307, 308}:
                    location = error.headers.get("Location")
                    error.close()
                    if not location:
                        raise SubmissionError(
                            "下载重定向缺少目标地址 / Download redirect has no target"
                        ) from error
                    current = urllib.parse.urljoin(current, location)
                    continue
                error_type = (
                    TransientError
                    if error.code in {403, 408, 425, 429} or error.code >= 500
                    else SubmissionError
                )
                raise error_type(
                    f"下载请求返回 HTTP {error.code} / Download returned HTTP {error.code}"
                ) from error
            except (OSError, urllib.error.URLError) as error:
                raise TransientError(
                    f"无法连接下载地址 / Could not connect to the download URL: {error}"
                ) from error

            with response:
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > max_size:
                            raise SubmissionError(
                                f"下载内容超过 {max_size} 字节限制 / Download exceeds the {max_size}-byte limit"
                            )
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(min(1 << 20, max_size + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_size:
                        raise SubmissionError(
                            f"下载内容超过 {max_size} 字节限制 / Download exceeds the {max_size}-byte limit"
                        )
                return b"".join(chunks)

        raise SubmissionError(
            f"下载重定向超过 {MAX_REDIRECTS} 次 / Download exceeded {MAX_REDIRECTS} redirects"
        )


@dataclass
class SubmissionResult:
    submission_type: str
    plugin: dict[str, Any]
    asset_name: str
    release_tag: str | None = None


def parse_issue_form(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in FORM_FIELD_PATTERN.finditer(body or ""):
        value = match.group("value").strip()
        if value == "_No response_":
            value = ""
        fields[match.group("label").strip()] = value
    return fields


def required_field(fields: dict[str, str], label: str) -> str:
    value = fields.get(label, "").strip()
    if not value:
        raise SubmissionError(f"缺少必填字段“{label}” / Missing required field: {label}")
    return value


def require_checked_confirmations(
    fields: dict[str, str], label: str, required_texts: list[str]
) -> None:
    value = required_field(fields, label)
    checked = [
        line
        for line in value.splitlines()
        if re.match(r"^- \[[xX]\] ", line.strip())
    ]
    for required_text in required_texts:
        if not any(required_text in line for line in checked):
            raise SubmissionError(
                f"请勾选确认项：{required_text} / Required confirmation was not checked"
            )


def validate_http_url(value: str, field: str) -> urllib.parse.ParseResult:
    try:
        parsed = urllib.parse.urlparse(value)
        _ = parsed.port
    except ValueError as error:
        raise SubmissionError(f"{field} 不是有效 URL / {field} is not a valid URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SubmissionError(
            f"{field} 必须是无用户信息的 HTTP(S) URL / {field} must be an HTTP(S) URL without credentials"
        )
    return parsed


def validate_public_url(value: str) -> None:
    parsed = validate_http_url(value, "下载地址 / Download URL")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise SubmissionError(
            f"下载地址域名无法解析 / Download hostname could not be resolved: {error}"
        ) from error
    if not addresses:
        raise SubmissionError("下载地址域名没有可用地址 / Download hostname has no usable address")
    for address_info in addresses:
        address = ipaddress.ip_address(address_info[4][0])
        if not address.is_global:
            raise SubmissionError(
                "下载地址解析到私有或内部网络 / Download URL resolves to a private or internal network"
            )


def parse_github_repository(value: str) -> tuple[str, str]:
    parsed = validate_http_url(value, "GitHub 仓库地址 / GitHub repository URL")
    if parsed.hostname.lower() not in {"github.com", "www.github.com"}:
        raise SubmissionError(
            "GitHub 仓库地址必须使用 github.com / GitHub repository URL must use github.com"
        )
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parsed.query or parsed.fragment:
        raise SubmissionError(
            "请填写仓库根地址，例如 https://github.com/owner/repository / Enter the repository root URL"
        )
    owner = parts[0]
    repository = re.sub(r"\.git$", "", parts[1], flags=re.IGNORECASE)
    if not owner or not repository:
        raise SubmissionError("GitHub 仓库地址不完整 / GitHub repository URL is incomplete")
    return owner, repository


def reject_github_hosted_url(value: str, field: str) -> None:
    parsed = validate_http_url(value, field)
    hostname = parsed.hostname.lower()
    if hostname in GITHUB_HOSTS or hostname.endswith(".githubusercontent.com"):
        raise SubmissionError(
            f"{field} 属于 GitHub；请使用 GitHub 插件模板 / {field} is GitHub-hosted; use the GitHub plugin form"
        )


def validate_short(value: str) -> str:
    short = value.strip()
    if short == "default" or not SHORT_PATTERN.fullmatch(short):
        raise SubmissionError(
            "插件唯一短名称只能包含字母、数字、下划线和连字符，且不能为 default / "
            "The unique short name may only contain letters, numbers, underscores and hyphens, and cannot be default"
        )
    return short


def required_manifest_text(manifest: dict[str, Any], field: str) -> str:
    value = manifest.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SubmissionError(
            f"komari-plugin.json 缺少有效的 {field} / komari-plugin.json is missing a valid {field}"
        )
    return value.strip()


def inspect_plugin_package(package_data: bytes) -> dict[str, str]:
    try:
        archive = zipfile.ZipFile(BytesIO(package_data))
    except (OSError, zipfile.BadZipFile) as error:
        raise SubmissionError(f"插件包不是有效 ZIP / Plugin package is not a valid ZIP: {error}") from error

    with archive:
        files = archive.infolist()
        if len(files) > MAX_ARCHIVE_FILES:
            raise SubmissionError(
                f"插件包文件数超过 {MAX_ARCHIVE_FILES} / Plugin package contains more than {MAX_ARCHIVE_FILES} files"
            )

        extracted_size = 0
        manifests: list[zipfile.ZipInfo] = []
        for entry in files:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (
                entry.filename.startswith(("/", "\\"))
                or "\\" in entry.filename
                or "\x00" in entry.filename
                or re.match(r"^[A-Za-z]:", entry.filename)
                or ".." in path.parts
                or stat.S_ISLNK(mode)
            ):
                raise SubmissionError(
                    f"插件包包含不安全路径：{entry.filename} / Plugin package contains an unsafe path"
                )
            if entry.is_dir():
                continue
            if entry.file_size > MAX_ARCHIVE_FILE_SIZE:
                raise SubmissionError(
                    f"插件文件超过 {MAX_ARCHIVE_FILE_SIZE} 字节：{entry.filename} / Plugin file is too large"
                )
            extracted_size += entry.file_size
            if extracted_size > MAX_EXTRACTED_SIZE:
                raise SubmissionError(
                    f"插件包解压后超过 {MAX_EXTRACTED_SIZE} 字节 / Extracted plugin exceeds the size limit"
                )
            if entry.filename == "komari-plugin.json":
                manifests.append(entry)

        if len(manifests) != 1:
            raise SubmissionError(
                "插件包根目录必须且只能包含一个 komari-plugin.json / "
                "Plugin package root must contain exactly one komari-plugin.json"
            )
        manifest_info = manifests[0]
        if manifest_info.file_size > MAX_MANIFEST_SIZE:
            raise SubmissionError(
                f"komari-plugin.json 超过 {MAX_MANIFEST_SIZE} 字节 / komari-plugin.json is too large"
            )
        try:
            manifest_data = archive.read(manifest_info)
            manifest = json.loads(manifest_data)
        except (RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SubmissionError(
                f"komari-plugin.json 无法读取或格式无效 / komari-plugin.json is unreadable or invalid: {error}"
            ) from error

    if not isinstance(manifest, dict):
        raise SubmissionError("komari-plugin.json 必须是 JSON 对象 / komari-plugin.json must be a JSON object")
    name = required_manifest_text(manifest, "name")
    short = validate_short(required_manifest_text(manifest, "short"))
    version = required_manifest_text(manifest, "version")
    author = required_manifest_text(manifest, "author")
    description = manifest.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise SubmissionError(
            "komari-plugin.json 的 description 必须是字符串 / komari-plugin.json description must be a string"
        )
    komari = manifest.get("komari", "")
    if not isinstance(komari, str):
        raise SubmissionError(
            "komari-plugin.json 的 komari 必须是字符串 / komari-plugin.json komari must be a string"
        )
    return {
        "name": name,
        "short": short,
        "description": description.strip(),
        "version": version,
        "author": author,
        "komari": komari.strip(),
    }


def process_github_submission(
    fields: dict[str, str], client: HTTPClient
) -> SubmissionResult:
    repository_input = required_field(fields, GITHUB_REPOSITORY_FIELD)
    require_checked_confirmations(
        fields,
        GITHUB_CONFIRMATION_FIELD,
        [PUBLIC_RELEASE_CONFIRMATION],
    )
    owner, repository = parse_github_repository(repository_input)

    repo = client.get_github_json(
        f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repository, safe='')}"
    )
    if repo.get("private"):
        raise SubmissionError("GitHub 仓库不是公开仓库 / GitHub repository is not public")
    repository_url = repo.get("html_url")
    if not isinstance(repository_url, str):
        raise SubmissionError("无法确认 GitHub 仓库地址 / Could not confirm GitHub repository URL")

    release = client.get_github_json(
        f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repository, safe='')}/releases/latest"
    )
    release_tag = release.get("tag_name")
    if not isinstance(release_tag, str) or not release_tag:
        raise SubmissionError("最新 GitHub Release 缺少标签 / Latest GitHub Release has no tag")
    assets = release.get("assets")
    if not isinstance(assets, list):
        assets = []
    zip_assets = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and isinstance(asset.get("name"), str)
        and asset["name"].lower().endswith(".zip")
        and isinstance(asset.get("browser_download_url"), str)
    ]
    if not zip_assets:
        raise SubmissionError(
            "最新 GitHub Release 没有 ZIP 资源 / Latest GitHub Release has no ZIP asset"
        )

    valid_packages: list[tuple[dict[str, Any], bytes, dict[str, str]]] = []
    failures: list[str] = []
    for asset in zip_assets:
        try:
            package_data = client.download(asset["browser_download_url"])
            manifest = inspect_plugin_package(package_data)
            valid_packages.append((asset, package_data, manifest))
        except SubmissionError as error:
            failures.append(f"{asset['name']}: {error}")

    if not valid_packages:
        detail = "; ".join(failures)
        raise SubmissionError(
            f"最新 Release 中没有通过校验的插件 ZIP / No plugin ZIP in the latest Release passed validation. {detail}"
        )
    if len(valid_packages) > 1:
        names = ", ".join(asset["name"] for asset, _, _ in valid_packages)
        raise SubmissionError(
            f"最新 Release 中有多个有效插件 ZIP，无法自动选择：{names} / "
            f"Multiple valid plugin ZIPs were found and selection is ambiguous: {names}"
        )

    asset, package_data, manifest = valid_packages[0]
    plugin = {
        **manifest,
        "url": repository_url,
        "download": asset["browser_download_url"],
        "sha256": hashlib.sha256(package_data).hexdigest(),
    }
    return SubmissionResult("github", plugin, asset["name"], release_tag)


def process_external_submission(
    fields: dict[str, str], client: HTTPClient
) -> SubmissionResult:
    project_url = required_field(fields, PROJECT_URL_FIELD)
    download = required_field(fields, DOWNLOAD_FIELD)
    name = required_field(fields, NAME_FIELD)
    short = validate_short(required_field(fields, SHORT_FIELD))
    version = required_field(fields, VERSION_FIELD)
    description = required_field(fields, DESCRIPTION_FIELD)
    author = required_field(fields, AUTHOR_FIELD)
    require_checked_confirmations(
        fields,
        EXTERNAL_CONFIRMATION_FIELD,
        [NO_MALICIOUS_CODE_CONFIRMATION, MANUAL_UPDATE_CONFIRMATION],
    )

    reject_github_hosted_url(project_url, "项目地址 / Project URL")
    reject_github_hosted_url(download, "插件包下载地址 / Plugin package URL")

    package_data = client.download(download)
    manifest = inspect_plugin_package(package_data)
    if manifest["short"] != short:
        raise SubmissionError(
            f"插件唯一短名称与 komari-plugin.json 不一致：{short} != {manifest['short']} / "
            "Unique short name does not match komari-plugin.json"
        )
    if manifest["version"] != version:
        raise SubmissionError(
            f"插件版本与 komari-plugin.json 不一致：{version} != {manifest['version']} / "
            "Plugin version does not match komari-plugin.json"
        )

    asset_name = PurePosixPath(urllib.parse.urlparse(download).path).name or "plugin.zip"
    plugin = {
        "name": name,
        "short": short,
        "description": description,
        "version": version,
        "author": author,
        "url": project_url,
        "download": download,
        "sha256": hashlib.sha256(package_data).hexdigest(),
        "komari": manifest.get("komari", ""),
    }
    return SubmissionResult("external", plugin, asset_name)


def add_plugin_to_catalog(catalog: dict[str, Any], plugin: dict[str, Any]) -> None:
    plugins = catalog.get("plugins")
    if catalog.get("schema") != 1 or not isinstance(plugins, list):
        raise RuntimeError("v1.json must contain schema 1 and a plugins array")
    short_key = plugin["short"].upper()
    for existing in plugins:
        if not isinstance(existing, dict):
            raise RuntimeError("v1.json contains an invalid plugin entry")
        if str(existing.get("short", "")).upper() == short_key:
            raise SubmissionError(
                f"插件唯一短名称 {plugin['short']} 已存在 / Plugin short name {plugin['short']} already exists"
            )
        if existing.get("url") == plugin["url"]:
            raise SubmissionError(
                "该项目地址已经存在于市场中 / This project URL is already listed in the market"
            )
        if existing.get("download") == plugin["download"]:
            raise SubmissionError(
                "该插件包下载地址已经存在于市场中 / This package URL is already listed in the market"
            )
    plugins.append(plugin)
    plugins.sort(key=lambda item: (str(item.get("short", "")).upper(), str(item.get("short", ""))))
    catalog["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def inline_code(value: Any) -> str:
    text = str(value).replace("`", "'").replace("|", "\\|")
    text = " ".join(text.splitlines())
    return f"`{text}`"


def render_success_comment(result: SubmissionResult) -> str:
    plugin = result.plugin
    lines = [
        "<!-- plugin-submission-validation -->",
        "## 自动检查通过 / Automated checks passed",
        "",
        f"- 插件 / Plugin: {inline_code(plugin['name'])}",
        f"- 唯一短名称 / Unique short name: {inline_code(plugin['short'])}",
        f"- 版本 / Version: {inline_code(plugin['version'])}",
        f"- 插件包 / Package: {inline_code(result.asset_name)}",
        f"- SHA-256: {inline_code(plugin['sha256'])}",
    ]
    if plugin.get("komari"):
        lines.append(f"- 支持版本 / Komari constraint: {inline_code(plugin['komari'])}")
    if result.release_tag:
        lines.append(f"- GitHub Release: {inline_code(result.release_tag)}")
    lines.extend(
        [
            "",
            "插件包结构、根目录 `komari-plugin.json`、必填元数据、唯一短名称和版本已经通过检查。",
            "",
            "The package structure, root `komari-plugin.json`, required metadata, unique short name and version passed validation.",
        ]
    )
    return "\n".join(lines)


def render_failure_comment(reason: str) -> str:
    quoted_reason = "\n".join(f"> {line}" for line in str(reason).splitlines())
    return "\n".join(
        [
            "## 自动检查失败 / Automated checks failed",
            "",
            "本次插件提交无法继续处理。 / This plugin submission cannot be processed.",
            "",
            "**原因 / Reason**",
            "",
            quoted_reason,
            "",
            "Issue 已自动关闭。你可以修改原 Issue，然后重新打开；Action 会重新检查，不需要新建 Issue。",
            "",
            "This issue was closed automatically. Edit the original issue and reopen it; the Action will check it again. You do not need to create a new issue.",
        ]
    )


def render_retry_comment(reason: str) -> str:
    quoted_reason = "\n".join(f"> {line}" for line in str(reason).splitlines())
    return "\n".join(
        [
            "## 自动检查暂时无法完成 / Automated checks could not complete",
            "",
            "检测遇到临时网络或 GitHub 服务错误，Issue 将保持开启。",
            "",
            "A temporary network or GitHub service error interrupted validation. The issue remains open.",
            "",
            "**原因 / Reason**",
            "",
            quoted_reason,
        ]
    )


def render_pr_body(result: SubmissionResult, issue_number: int) -> str:
    plugin = result.plugin
    source_label = "GitHub Release" if result.submission_type == "github" else "External package"
    return "\n".join(
        [
            "## Plugin submission / 插件上架",
            "",
            f"Adds [{plugin['short']}]({plugin['url']}) from issue #{issue_number}.",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Name | {inline_code(plugin['name'])} |",
            f"| Short | {inline_code(plugin['short'])} |",
            f"| Version | {inline_code(plugin['version'])} |",
            f"| Author | {inline_code(plugin['author'])} |",
            f"| Komari constraint | {inline_code(plugin.get('komari') or 'Any')} |",
            f"| Source type | {source_label} |",
            f"| Package | {inline_code(result.asset_name)} |",
            f"| SHA-256 | {inline_code(plugin['sha256'])} |",
            "",
            "### Automated verification / 自动校验",
            "",
            "- [x] Package download completed within the catalog size limit",
            "- [x] ZIP paths, file count and extracted sizes passed safety checks",
            "- [x] A valid root `komari-plugin.json` was found",
            "- [x] Required metadata and unique short name passed validation",
            "- [x] Catalog order and duplicate checks passed",
            "- [x] SHA-256 was calculated from the verified ZIP",
            "",
            f"Closes #{issue_number}",
            "",
            f"<!-- plugin-submission-issue: {issue_number} -->",
        ]
    )


def write_github_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise RuntimeError(f"GitHub output {key} must be a single line")
            output.write(f"{key}={value}\n")


def process_event(
    event: dict[str, Any], catalog: dict[str, Any], client: HTTPClient
) -> tuple[SubmissionResult, int]:
    issue = event.get("issue")
    if not isinstance(issue, dict):
        raise RuntimeError("event does not contain an issue")
    issue_number = issue.get("number")
    if not isinstance(issue_number, int):
        raise RuntimeError("event issue does not contain a valid number")
    fields = parse_issue_form(str(issue.get("body") or ""))
    has_github = GITHUB_REPOSITORY_FIELD in fields
    has_external = DOWNLOAD_FIELD in fields
    if has_github == has_external:
        raise SubmissionError(
            "无法识别提交模板，请使用仓库提供的插件 Issue Form / "
            "Could not identify the submission form; use a plugin Issue Form"
        )
    result = (
        process_github_submission(fields, client)
        if has_github
        else process_external_submission(fields, client)
    )
    add_plugin_to_catalog(catalog, result.plugin)
    return result, issue_number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process a Komari plugin submission issue")
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--comment", type=Path, required=True)
    parser.add_argument("--pr-body", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args(argv)

    event = json.loads(args.event.read_text(encoding="utf-8"))
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    client = HTTPClient(os.environ.get("GITHUB_TOKEN"))
    try:
        result, issue_number = process_event(event, catalog, client)
    except SubmissionError as error:
        args.comment.write_text(f"{render_failure_comment(str(error))}\n", encoding="utf-8")
        write_github_outputs(args.github_output, {"status": "failure"})
        print(f"Submission rejected: {error}")
        return 0
    except TransientError as error:
        args.comment.write_text(f"{render_retry_comment(str(error))}\n", encoding="utf-8")
        write_github_outputs(args.github_output, {"status": "retry"})
        print(f"Submission check should be retried: {error}")
        return 0

    args.catalog.write_text(f"{json.dumps(catalog, ensure_ascii=False, indent=2)}\n", encoding="utf-8")
    args.comment.write_text(f"{render_success_comment(result)}\n", encoding="utf-8")
    args.pr_body.write_text(f"{render_pr_body(result, issue_number)}\n", encoding="utf-8")
    write_github_outputs(
        args.github_output,
        {
            "status": "success",
            "short": result.plugin["short"],
            "submission_type": result.submission_type,
        },
    )
    print(f"Submission validated: {result.plugin['short']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
