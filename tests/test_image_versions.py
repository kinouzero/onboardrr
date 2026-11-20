import unittest
from unittest.mock import Mock, patch

from onboardrr.image_versions import docker_hub_tags, resolve_images, select_tag


def tag(name, architecture="arm64", *, variant=None):
    return {
        "name": name,
        "images": [{"os": "linux", "architecture": architecture, "variant": variant}],
    }


class ImageVersionsTest(unittest.TestCase):
    def test_compose_labels_filter_newer_compatible_versions_and_preserve_source(self):
        compose = r"""services:
  exporter:
    image: "prom/node-exporter:v1.2.0" # keep this
    labels:
      wud.tag.include: '^v$${major}\.\d+\.\d+$$'
      wud.tag.exclude: '^v1\.11\.0$$'
  latest:
    image: alpine/socat:latest
    labels: ['wud.tag.include=latest']
  disabled:
    image: prom/node-exporter:v1.2.0
    labels: ['wud.tag.include=.*', 'wud.watch=false']
  immutable:
    image: prom/node-exporter:v1.2.0@sha256:abc
    labels: ['wud.tag.include=.*']
"""
        entries = [tag(name) for name in ("v1.9.0", "v1.10.0", "v1.11.0", "v2.0.0", "v1.12.0-rc1")]
        entries.append(tag("v1.12.0", "amd64"))
        cache = {"prom/node-exporter": entries}
        host = Mock(return_value="arm64")
        resolved, updates = resolve_images(compose, host, cache)
        self.assertEqual(resolved, compose.replace('image: "prom/node-exporter:v1.2.0"',
                                                   'image: "prom/node-exporter:v1.10.0"'))
        self.assertEqual(updates, {"exporter": {"from": "prom/node-exporter:v1.2.0",
                                                "to": "prom/node-exporter:v1.10.0"}})
        host.assert_called_once()

    def test_transformed_tags_compare_versions_without_replacing_equal_builds(self):
        labels = {
            "wud.tag.include": r"^\d+\.\d+\.\d+-\d+-[a-z]+$$",
            "wud.tag.transform": r"^(\d+\.\d+\.\d+-\d+)-[a-z]+$$ => $$1",
        }
        self.assertEqual(select_tag([tag("1.0.0-9-b")], "1.0.0-9-a", labels, ["linux", "arm64"]), "1.0.0-9-a")
        self.assertEqual(select_tag([tag("1.0.0-10-c")], "1.0.0-9-a", labels, ["linux", "arm64"]), "1.0.0-10-c")

    def test_stable_release_does_not_change_to_prerelease_or_calendar_version(self):
        tags = [tag(name) for name in ("1.1.0", "2.0.0-beta2", "2026.11.20", "1.2.3.sig")]
        self.assertEqual(select_tag(tags, "1.2.0", {"wud.tag.include": ".*"}, ["linux", "arm64"]), "1.2.0")

    def test_explicit_platform_overrides_server_and_preserves_variant(self):
        host = Mock()
        compose = """services:
  service:
    image: ubuntu:1.0
    platform: linux/arm/v7
    labels: ['wud.tag.include=.*']
"""
        cache = {"library/ubuntu": [tag("1.2", "arm", variant="v6"), tag("1.1", "arm", variant="v7")]}
        result, _ = resolve_images(compose, host, cache)
        self.assertIn("image: ubuntu:1.1", result)
        host.assert_not_called()

    @patch("onboardrr.image_versions.DOCKER_HUB")
    def test_tag_listing_reads_all_pages_using_the_configured_origin(self, api):
        api.request.side_effect = [
            {"next": "https://other.example/anything", "results": [tag("1.0.0")]},
            {"next": None, "results": [tag("2.0.0")]},
        ]
        self.assertEqual(list(docker_hub_tags("library/alpine")), [tag("1.0.0"), tag("2.0.0")])
        self.assertEqual([call.args[1] for call in api.request.call_args_list], [
            "/v2/namespaces/library/repositories/alpine/tags?page_size=100&page=1",
            "/v2/namespaces/library/repositories/alpine/tags?page_size=100&page=2",
        ])

    @patch("onboardrr.image_versions.docker_hub_tags")
    def test_unsupported_registry_fails_without_network_request(self, tags):
        with self.assertRaisesRegex(ValueError, "public Docker Hub image"):
            resolve_images("services: {app: {image: private.example/dev/app:1.0.0, "
                           "labels: ['wud.tag.include=.*']}}", Mock(), {})
        tags.assert_not_called()
