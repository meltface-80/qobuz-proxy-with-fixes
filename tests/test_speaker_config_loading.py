"""Tests for loading speaker configs: load_config, config path, device type and gapless."""

from pathlib import Path

import pytest

from qobuz_proxy.config import (
    Config,
    ConfigError,
    SpeakerConfig,
    _parse_env_speakers,
    _parse_yaml_speakers,
    _single_speaker_from_config,
    build_speaker_configs,
    load_config,
)


class TestLoadConfigSpeakers:
    def test_yaml_speakers_key_extracted(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
qobuz:
  email: test@example.com
  password: secret
speakers:
  - name: Room A
    backend: dlna
    dlna_ip: 192.168.1.10
  - name: Room B
    backend: dlna
    dlna_ip: 192.168.1.11
""")
        config = load_config(config_path=config_file)
        assert len(config.speakers) == 2
        assert config.speakers[0].name == "Room A"
        assert config.speakers[1].name == "Room B"

    def test_flat_config_produces_one_speaker(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
qobuz:
  email: test@example.com
  password: secret
device:
  name: Single Speaker
backend:
  type: dlna
  dlna:
    ip: 192.168.1.50
""")
        config = load_config(config_path=config_file)
        assert len(config.speakers) == 1
        assert config.speakers[0].name == "Single Speaker"
        assert config.speakers[0].dlna_ip == "192.168.1.50"

    def test_build_speaker_configs_yaml_priority_over_env(self, monkeypatch):
        monkeypatch.setenv("QOBUZPROXY_DEVICE_NAME", "Env Speaker")
        config = Config()
        raw_yaml = [{"name": "YAML Speaker", "dlna_ip": "10.0.0.1"}]
        speakers = build_speaker_configs(config, raw_yaml_speakers=raw_yaml)
        assert len(speakers) == 1
        assert speakers[0].name == "YAML Speaker"

    def test_build_speaker_configs_env_over_flat(self, monkeypatch):
        monkeypatch.setenv("QOBUZPROXY_DEVICE_NAME", "Env A,Env B")
        monkeypatch.setenv("QOBUZPROXY_DLNA_IP", "10.0.0.1,10.0.0.2")
        config = Config()
        speakers = build_speaker_configs(config)
        assert len(speakers) == 2
        assert speakers[0].name == "Env A"
        assert speakers[1].name == "Env B"

    def test_build_speaker_configs_fallback_to_flat(self, monkeypatch):
        monkeypatch.delenv("QOBUZPROXY_DEVICE_NAME", raising=False)
        config = Config()
        config.device.name = "Flat Speaker"
        config.backend.dlna.ip = "192.168.1.50"
        config.server.http_port = 8689
        config.backend.dlna.proxy_port = 7120
        speakers = build_speaker_configs(config)
        assert len(speakers) == 1
        assert speakers[0].name == "Flat Speaker"


class TestConfigPathFallback:
    """When no config file exists, the writable fallback path must respect
    QOBUZPROXY_DATA_DIR. Otherwise Docker users (CWD=/app, /data mounted)
    silently lose every speaker change made via the web UI.
    """

    def test_data_dir_used_when_no_config_file(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("QOBUZPROXY_DATA_DIR", str(data_dir))

        config = load_config()

        assert config.config_path == data_dir / "config.yaml"

    def test_local_cwd_used_when_no_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("QOBUZPROXY_DATA_DIR", raising=False)

        config = load_config()

        assert config.config_path == Path("./config.yaml")

    def test_explicit_path_overrides_data_dir(self, monkeypatch, tmp_path):
        explicit = tmp_path / "custom.yaml"
        explicit.write_text("server:\n  http_port: 9000\n")
        monkeypatch.setenv("QOBUZPROXY_DATA_DIR", str(tmp_path / "data"))

        config = load_config(config_path=explicit)

        assert config.config_path == explicit


class TestDeviceType:
    def test_device_types_match_proto_enum(self):
        from qobuz_proxy.config import DEVICE_TYPES
        from qobuz_proxy.proto import qconnect_common_pb2 as common_pb2

        # The .proto keeps StreamCore32's names; the config uses the web player's names
        proto_names = {
            "streamer": "SPEAKERBOX",
            "soundbar": "SPEAKERBOX2",
            "computer": "LAPTOP",
            "mobile": "PHONE",
            "cast": "GOOGLE_CAST",
        }
        for name, value in DEVICE_TYPES.items():
            proto_name = proto_names.get(name, name.upper())
            assert common_pb2.DeviceType.Value(f"DEVICE_TYPE_{proto_name}") == value

    def test_default_is_speaker(self):
        assert SpeakerConfig().device_type == "speaker"
        assert Config().device.device_type == "speaker"

    def test_yaml_speaker_device_type(self):
        speakers = _parse_yaml_speakers(
            [{"name": "Phones", "backend": "local", "device_type": "Headphones"}, {"name": "A"}],
            Config(),
        )
        assert speakers[0].device_type == "headphones"
        assert speakers[1].device_type == "speaker"

    def test_yaml_speaker_invalid_device_type(self):
        with pytest.raises(ConfigError, match="Invalid device_type"):
            _parse_yaml_speakers([{"name": "A", "device_type": "toaster"}], Config())

    def test_env_speakers_device_type(self, monkeypatch):
        monkeypatch.setenv("QOBUZPROXY_DEVICE_NAME", "A,B")
        monkeypatch.setenv("QOBUZPROXY_DEVICE_TYPE", "tv,headphones")
        speakers = _parse_env_speakers(Config())
        assert [s.device_type for s in speakers] == ["tv", "headphones"]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("tv,headphones", ["tv", "headphones"]), ("headphones", ["headphones", "headphones"])],
    )
    def test_load_config_multi_speaker_device_types(self, monkeypatch, tmp_path, value, expected):
        monkeypatch.setenv("QOBUZPROXY_DEVICE_NAME", "A,B")
        monkeypatch.setenv("QOBUZPROXY_BACKEND", "local")
        monkeypatch.setenv("QOBUZPROXY_DEVICE_TYPE", value)

        config = load_config(config_path=tmp_path / "config.yaml")

        assert [s.device_type for s in config.speakers] == expected

    @pytest.mark.parametrize(
        ("value", "error"),
        [
            ("tv,toaster", "Invalid device_type: 'toaster'"),
            ("tv,headphones,tablet", "expected 1 or 2"),
        ],
    )
    def test_load_config_rejects_invalid_multi_speaker_device_types(
        self, monkeypatch, tmp_path, value, error
    ):
        monkeypatch.setenv("QOBUZPROXY_DEVICE_NAME", "A,B")
        monkeypatch.setenv("QOBUZPROXY_BACKEND", "local")
        monkeypatch.setenv("QOBUZPROXY_DEVICE_TYPE", value)

        with pytest.raises(ConfigError, match=error):
            load_config(config_path=tmp_path / "config.yaml")

    def test_flat_config_device_type(self):
        from qobuz_proxy.config import dict_to_config

        config = dict_to_config(
            {"device": {"device_type": "computer"}, "backend": {"type": "local"}}
        )
        assert _single_speaker_from_config(config).device_type == "computer"

    def test_flat_config_invalid_device_type(self):
        from qobuz_proxy.config import dict_to_config

        with pytest.raises(ConfigError, match="Invalid device_type"):
            dict_to_config({"device": {"device_type": "toaster"}})

    def test_round_trip_through_config_dict(self):
        from qobuz_proxy.config import speaker_config_to_dict

        sc = SpeakerConfig(name="Phones", backend_type="local", device_type="headphones")
        d = speaker_config_to_dict(sc)
        assert d["device_type"] == "headphones"
        assert _parse_yaml_speakers([d], Config())[0].device_type == "headphones"
        # The default is not written, so existing config files stay unchanged
        assert "device_type" not in speaker_config_to_dict(SpeakerConfig())


class TestDlnaGaplessSetting:
    """Per-speaker gapless setting: "auto" (default), "on" or "off"."""

    def test_yaml_speaker_gapless_off(self) -> None:
        from qobuz_proxy.config import Config, _parse_yaml_speakers

        speakers = _parse_yaml_speakers(
            [{"name": "Desk", "dlna_ip": "10.0.0.5", "dlna_gapless": False}], Config()
        )
        assert speakers[0].dlna_gapless == "off"

    def test_default_is_auto_and_not_written(self) -> None:
        from qobuz_proxy.config import Config, _parse_yaml_speakers, speaker_config_to_dict

        sc = _parse_yaml_speakers([{"name": "Desk", "dlna_ip": "10.0.0.5"}], Config())[0]
        assert sc.dlna_gapless == "auto"
        assert "dlna_gapless" not in speaker_config_to_dict(sc)

    def test_round_trip(self) -> None:
        from qobuz_proxy.config import Config, _parse_yaml_speakers, speaker_config_to_dict

        sc = _parse_yaml_speakers(
            [{"name": "Desk", "dlna_ip": "10.0.0.5", "dlna_gapless": "off"}], Config()
        )[0]
        assert speaker_config_to_dict(sc)["dlna_gapless"] == "off"
