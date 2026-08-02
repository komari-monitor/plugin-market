import hashlib
import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock

from scripts import plugin_submission as submission


def make_plugin_zip(manifest=None, extra_entries=None):
    manifest = manifest or {
        "name": "Test Plugin",
        "short": "TestPlugin",
        "description": "A test plugin",
        "version": "1.2.3",
        "author": "Test Author",
        "komari": ">=1.0.0",
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("komari-plugin.json", json.dumps(manifest))
        archive.writestr("dist/index.html", "<!doctype html>")
        for name, value in extra_entries or []:
            archive.writestr(name, value)
    return output.getvalue()


def issue_field(label, value):
    return f"### {label}\n\n{value}\n\n"


def github_issue_body(repository="https://github.com/example/plugin"):
    return "".join(
        [
            issue_field(submission.GITHUB_REPOSITORY_FIELD, repository),
            issue_field(
                submission.GITHUB_CONFIRMATION_FIELD,
                "- [x] "
                + submission.PUBLIC_RELEASE_CONFIRMATION
                + " / The repository must be public.",
            ),
        ]
    )


def external_issue_body(**overrides):
    values = {
        submission.PROJECT_URL_FIELD: "https://plugins.example.com/test",
        submission.DOWNLOAD_FIELD: "https://downloads.example.com/test.zip",
        submission.NAME_FIELD: "Test Plugin Listing",
        submission.SHORT_FIELD: "TestPlugin",
        submission.VERSION_FIELD: "1.2.3",
        submission.DESCRIPTION_FIELD: "Catalog description",
        submission.AUTHOR_FIELD: "Catalog Author",
        submission.EXTERNAL_CONFIRMATION_FIELD: "\n".join(
            [
                "- [x] " + submission.NO_MALICIOUS_CODE_CONFIRMATION,
                "- [X] " + submission.MANUAL_UPDATE_CONFIRMATION,
            ]
        ),
    }
    values.update(overrides)
    return "".join(issue_field(label, value) for label, value in values.items())


class FakeClient:
    def __init__(self, github=None, downloads=None):
        self.github = github or {}
        self.downloads = downloads or {}

    def get_github_json(self, path):
        value = self.github[path]
        if isinstance(value, Exception):
            raise value
        return value

    def download(self, url):
        value = self.downloads[url]
        if isinstance(value, Exception):
            raise value
        return value


class IssueFormTests(unittest.TestCase):
    def test_parse_issue_form_handles_crlf_and_no_response(self):
        body = (
            issue_field(submission.NAME_FIELD, "Test Plugin")
            + issue_field(submission.DESCRIPTION_FIELD, "_No response_")
        ).replace("\n", "\r\n")
        fields = submission.parse_issue_form(body)
        self.assertEqual(fields[submission.NAME_FIELD], "Test Plugin")
        self.assertEqual(fields[submission.DESCRIPTION_FIELD], "")

    def test_required_confirmation_must_be_checked(self):
        fields = submission.parse_issue_form(
            github_issue_body().replace("- [x]", "- [ ]")
        )
        with self.assertRaisesRegex(submission.SubmissionError, "请勾选确认项"):
            submission.require_checked_confirmations(
                fields,
                submission.GITHUB_CONFIRMATION_FIELD,
                [submission.PUBLIC_RELEASE_CONFIRMATION],
            )

    def test_repository_parser_normalizes_git_suffix(self):
        self.assertEqual(
            submission.parse_github_repository("https://www.github.com/Owner/Plugin.git/"),
            ("Owner", "Plugin"),
        )

    def test_repository_parser_rejects_subpaths(self):
        with self.assertRaises(submission.SubmissionError):
            submission.parse_github_repository("https://github.com/owner/plugin/releases")


class PackageTests(unittest.TestCase):
    def test_inspect_valid_package(self):
        manifest = submission.inspect_plugin_package(make_plugin_zip())
        self.assertEqual(manifest["short"], "TestPlugin")
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual(manifest["komari"], ">=1.0.0")

    def test_inspect_package_without_komari_defaults_to_empty(self):
        manifest = submission.inspect_plugin_package(
            make_plugin_zip(
                {
                    "name": "No Constraint",
                    "short": "NoConstraint",
                    "version": "1.0.0",
                    "author": "Author",
                }
            )
        )
        self.assertEqual(manifest["komari"], "")

    def test_non_string_komari_constraint_is_rejected(self):
        package = make_plugin_zip(
            {
                "name": "Bad Constraint",
                "short": "BadConstraint",
                "version": "1.0.0",
                "author": "Author",
                "komari": {"en": ">=1.0.0"},
            }
        )
        with self.assertRaisesRegex(submission.SubmissionError, "komari 必须是字符串"):
            submission.inspect_plugin_package(package)

    def test_manifest_must_be_at_archive_root(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("plugin/komari-plugin.json", "{}")
        with self.assertRaisesRegex(submission.SubmissionError, "根目录"):
            submission.inspect_plugin_package(output.getvalue())

    def test_unsafe_archive_path_is_rejected(self):
        package = make_plugin_zip(extra_entries=[("../outside.txt", "bad")])
        with self.assertRaisesRegex(submission.SubmissionError, "不安全路径"):
            submission.inspect_plugin_package(package)

    def test_default_short_is_rejected(self):
        package = make_plugin_zip(
            {
                "name": "Default",
                "short": "default",
                "version": "1.0.0",
                "author": "Author",
            }
        )
        with self.assertRaisesRegex(submission.SubmissionError, "不能为 default"):
            submission.inspect_plugin_package(package)

    def test_inspect_accepts_i18n_name_description_author(self):
        manifest = submission.inspect_plugin_package(
            make_plugin_zip(
                {
                    "name": {"zh-CN": "MJPEG 实时状态", "en": "MJPEG Live Status"},
                    "short": "mjpeg",
                    "description": {"zh-CN": "中文描述", "en": "English description"},
                    "version": "1.0.0",
                    "author": {"zh-CN": "作者", "en": "Author"},
                }
            )
        )
        self.assertEqual(manifest["name"]["en"], "MJPEG Live Status")
        self.assertEqual(manifest["author"]["zh-CN"], "作者")
        self.assertEqual(manifest["description"]["en"], "English description")

    def test_inspect_rejects_empty_i18n_name(self):
        package = make_plugin_zip(
            {
                "name": {"zh-CN": "", "en": "  "},
                "short": "Empty",
                "version": "1.0.0",
                "author": "Author",
            }
        )
        with self.assertRaisesRegex(submission.SubmissionError, "缺少有效的 name"):
            submission.inspect_plugin_package(package)

    def test_inspect_rejects_empty_i18n_description(self):
        package = make_plugin_zip(
            {
                "name": "N",
                "short": "N",
                "version": "1.0.0",
                "author": "A",
                "description": {"zh-CN": "", "en": ""},
            }
        )
        with self.assertRaisesRegex(submission.SubmissionError, "description 缺少非空内容"):
            submission.inspect_plugin_package(package)

    def test_inspect_rejects_non_text_description(self):
        package = make_plugin_zip(
            {
                "name": "N",
                "short": "N",
                "version": "1.0.0",
                "author": "A",
                "description": ["not", "text"],
            }
        )
        with self.assertRaisesRegex(submission.SubmissionError, "description"):
            submission.inspect_plugin_package(package)

    def test_display_text_resolves_i18n_objects(self):
        self.assertEqual(submission.display_text("Plain"), "Plain")
        self.assertEqual(submission.display_text({"en": "EN", "zh-CN": "ZH"}), "EN")
        self.assertEqual(submission.display_text({"zh-CN": "ZH", "ja": "JA"}), "ZH")
        self.assertEqual(submission.display_text({"ja": "JA"}), "JA")
        self.assertEqual(submission.display_text({}), "")


class SubmissionTests(unittest.TestCase):
    repo_path = "/repos/example/plugin"
    release_path = "/repos/example/plugin/releases/latest"

    def github_client(self, assets):
        return FakeClient(
            github={
                self.repo_path: {
                    "private": False,
                    "html_url": "https://github.com/example/plugin",
                },
                self.release_path: {"tag_name": "v1.2.3", "assets": assets},
            },
            downloads={
                asset["browser_download_url"]: asset["data"] for asset in assets
            },
        )

    def test_github_submission_selects_only_valid_zip(self):
        good = make_plugin_zip()
        assets = [
            {
                "name": "source.zip",
                "browser_download_url": "https://github.com/example/plugin/releases/download/v1/source.zip",
                "data": b"not a zip",
            },
            {
                "name": "plugin.zip",
                "browser_download_url": "https://github.com/example/plugin/releases/download/v1/plugin.zip",
                "data": good,
            },
        ]
        result = submission.process_github_submission(
            submission.parse_issue_form(github_issue_body()),
            self.github_client(assets),
        )
        self.assertEqual(result.plugin["short"], "TestPlugin")
        self.assertEqual(result.asset_name, "plugin.zip")
        self.assertEqual(result.release_tag, "v1.2.3")
        self.assertEqual(result.plugin["sha256"], hashlib.sha256(good).hexdigest())
        self.assertEqual(result.plugin["url"], "https://github.com/example/plugin")
        self.assertEqual(result.plugin["komari"], ">=1.0.0")

    def test_github_submission_rejects_multiple_valid_zips(self):
        package = make_plugin_zip()
        assets = [
            {
                "name": "one.zip",
                "browser_download_url": "https://github.com/example/plugin/releases/download/v1/one.zip",
                "data": package,
            },
            {
                "name": "two.ZIP",
                "browser_download_url": "https://github.com/example/plugin/releases/download/v1/two.ZIP",
                "data": package,
            },
        ]
        with self.assertRaisesRegex(submission.SubmissionError, "多个有效插件 ZIP"):
            submission.process_github_submission(
                submission.parse_issue_form(github_issue_body()),
                self.github_client(assets),
            )

    def test_github_submission_keeps_i18n_manifest_metadata(self):
        package = make_plugin_zip(
            {
                "name": {"zh-CN": "MJPEG 实时状态", "en": "MJPEG Live Status"},
                "short": "mjpeg",
                "description": {"zh-CN": "中文描述", "en": "English description"},
                "version": "1.0.0",
                "author": {"zh-CN": "作者", "en": "Author"},
            }
        )
        assets = [
            {
                "name": "mjpeg.zip",
                "browser_download_url": "https://github.com/example/plugin/releases/download/v1/mjpeg.zip",
                "data": package,
            }
        ]
        result = submission.process_github_submission(
            submission.parse_issue_form(github_issue_body()),
            self.github_client(assets),
        )
        self.assertEqual(result.plugin["name"]["en"], "MJPEG Live Status")
        self.assertIn("MJPEG Live Status", submission.render_success_comment(result))
        self.assertIn("MJPEG Live Status", submission.render_pr_body(result, 42))

    def test_private_github_repository_is_rejected(self):
        client = FakeClient(github={self.repo_path: {"private": True}})
        with self.assertRaisesRegex(submission.SubmissionError, "不是公开仓库"):
            submission.process_github_submission(
                submission.parse_issue_form(github_issue_body()), client
            )

    def test_external_submission_uses_form_metadata_and_package_hash(self):
        package = make_plugin_zip()
        url = "https://downloads.example.com/test.zip"
        result = submission.process_external_submission(
            submission.parse_issue_form(external_issue_body()),
            FakeClient(downloads={url: package}),
        )
        self.assertEqual(result.plugin["name"], "Test Plugin Listing")
        self.assertEqual(result.plugin["author"], "Catalog Author")
        self.assertEqual(result.plugin["sha256"], hashlib.sha256(package).hexdigest())
        self.assertEqual(result.plugin["komari"], ">=1.0.0")

    def test_external_manifest_version_must_match_form(self):
        package = make_plugin_zip()
        url = "https://downloads.example.com/test.zip"
        fields = submission.parse_issue_form(
            external_issue_body(**{submission.VERSION_FIELD: "9.9.9"})
        )
        with self.assertRaisesRegex(submission.SubmissionError, "版本与"):
            submission.process_external_submission(
                fields, FakeClient(downloads={url: package})
            )

    def test_external_github_project_url_is_rejected(self):
        fields = submission.parse_issue_form(
            external_issue_body(
                **{submission.PROJECT_URL_FIELD: "https://github.com/example/plugin"}
            )
        )
        with self.assertRaisesRegex(submission.SubmissionError, "请使用 GitHub 插件模板"):
            submission.process_external_submission(fields, FakeClient())


class CatalogAndOutputTests(unittest.TestCase):
    def plugin(self, short="TestPlugin"):
        return {
            "name": "Test Plugin",
            "short": short,
            "description": "Description",
            "version": "1.0.0",
            "author": "Author",
            "url": f"https://example.com/{short}",
            "download": f"https://example.com/{short}.zip",
            "sha256": "a" * 64,
        }

    def test_catalog_sorts_and_rejects_case_insensitive_duplicate(self):
        catalog = {"schema": 1, "plugins": [self.plugin("Zulu")]}
        submission.add_plugin_to_catalog(catalog, self.plugin("alpha"))
        self.assertEqual([item["short"] for item in catalog["plugins"]], ["alpha", "Zulu"])
        with self.assertRaisesRegex(submission.SubmissionError, "already exists"):
            submission.add_plugin_to_catalog(catalog, self.plugin("ALPHA"))

    def test_pr_body_links_issue_and_contains_lifecycle_marker(self):
        result = submission.SubmissionResult(
            "external", self.plugin(), "plugin.zip"
        )
        body = submission.render_pr_body(result, 42)
        self.assertIn("Closes #42", body)
        self.assertIn("<!-- plugin-submission-issue: 42 -->", body)
        self.assertIn("SHA-256", body)
        self.assertIn("| Komari constraint |", body)

    def test_success_comment_has_stable_refresh_marker(self):
        result = submission.SubmissionResult(
            "external", self.plugin(), "plugin.zip"
        )
        self.assertIn(
            "<!-- plugin-submission-validation -->",
            submission.render_success_comment(result),
        )

    def test_failure_comment_explains_reopen_flow(self):
        comment = submission.render_failure_comment("invalid package")
        self.assertIn("重新打开", comment)
        self.assertIn("不需要新建 Issue", comment)
        self.assertIn("reopen", comment)

    def test_retry_comment_keeps_issue_open(self):
        comment = submission.render_retry_comment("temporary error")
        self.assertIn("Issue 将保持开启", comment)
        self.assertIn("The issue remains open", comment)

    def test_rejected_cli_keeps_catalog_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_path = root / "event.json"
            catalog_path = root / "v1.json"
            comment_path = root / "comment.md"
            pr_body_path = root / "pr.md"
            output_path = root / "output.txt"
            event_path.write_text(
                json.dumps({"issue": {"number": 7, "body": github_issue_body()}}),
                encoding="utf-8",
            )
            original = {"schema": 1, "plugins": [self.plugin("TestPlugin")]}
            catalog_path.write_text(json.dumps(original), encoding="utf-8")
            package = make_plugin_zip()
            assets = [
                {
                    "name": "plugin.zip",
                    "browser_download_url": "https://github.com/example/plugin/releases/download/v1/plugin.zip",
                    "data": package,
                }
            ]
            fake = SubmissionTests().github_client(assets)
            with mock.patch.object(submission, "HTTPClient", return_value=fake):
                exit_code = submission.main(
                    [
                        "--event",
                        str(event_path),
                        "--catalog",
                        str(catalog_path),
                        "--comment",
                        str(comment_path),
                        "--pr-body",
                        str(pr_body_path),
                        "--github-output",
                        str(output_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(catalog_path.read_text()), original)
            self.assertIn("status=failure", output_path.read_text())
            self.assertIn("自动检查失败", comment_path.read_text(encoding="utf-8"))
            self.assertFalse(pr_body_path.exists())


if __name__ == "__main__":
    unittest.main()
