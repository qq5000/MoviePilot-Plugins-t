import os
import platform
import tarfile
import tempfile
import shutil
from typing import Any, List, Dict, Tuple
from pathlib import Path
from datetime import datetime, timedelta

import pytz
import psutil
import requests
from ruamel.yaml import YAML
from ruamel.yaml.representer import RoundTripRepresenter
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase


class MediaWarp(_PluginBase):
    # 插件名称
    plugin_name = "MediaWarp"
    # 插件描述
    plugin_desc = "EmbyServer/Jellyfin 中间件：优化播放 Strm 文件、自定义前端样式、自定义允许访问客户端、嵌入脚本。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/refs/heads/main/icons/cloud.png"
    # 插件版本
    plugin_version = "1.0.7"
    # 插件作者
    plugin_author = "DDSRem"
    # 作者主页
    author_url = "https://github.com/DDSRem"
    # 插件配置项ID前缀
    plugin_config_prefix = "mediawarp_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    _mediaserver_helper = None
    _mediaserver = None
    _mediaservers = None
    _emby_server = None
    _emby_host = None
    _emby_apikey = None
    # 私有属性
    _scheduler = None
    process = None
    _enabled = False
    _port = None
    _media_strm_path = None
    _crx = False
    _actor_plus = False
    _fanart_show = False
    _external_player_url = False
    _danmaku = False
    _video_together = False
    _srt2ass = False

    def __init__(self):
        """
        初始化
        """
        super().__init__()
        # 类名小写
        class_name = self.__class__.__name__.lower()
        # 二级制文件路径
        self.__mediawarp_path = settings.PLUGIN_DATA_PATH / class_name / "MediaWarp"
        # 配置文件路径
        self.__config_path = settings.PLUGIN_DATA_PATH / class_name / "config"
        # 日志路径
        self.__logs_dir = settings.PLUGIN_DATA_PATH / class_name / "logs"
        # 配置文件名
        self.__config_filename = "config.yaml"
        # 二级制文件版本
        self.__mediawarp_version = "0.1.12"
        self.__mediawarp_version_path = (
            settings.PLUGIN_DATA_PATH / class_name / "version.txt"
        )

    def init_plugin(self, config: dict = None):
        self._mediaserver_helper = MediaServerHelper()
        self._mediaserver = None

        if config:
            self._enabled = config.get("enabled")
            self._port = config.get("port")
            self._media_strm_path = config.get("media_strm_path")
            self._mediaservers = config.get("mediaservers") or []
            self._crx = config.get("crx")
            self._actor_plus = config.get("actor_plus")
            self._fanart_show = config.get("fanart_show")
            self._external_player_url = config.get("external_player_url")
            self._danmaku = config.get("danmaku")
            self._video_together = config.get("video_together")
            self._srt2ass = config.get("srt2ass")

            # 获取媒体服务器
            if self._mediaservers:
                self._mediaserver = [self._mediaservers[0]]

        # 获取媒体服务信息
        if self._mediaserver:
            emby_servers = self._mediaserver_helper.get_services(
                name_filters=self._mediaserver
            )

            for _, emby_server in emby_servers.items():
                self._emby_server = emby_server.type
                self._emby_apikey = emby_server.config.config.get("apikey")
                self._emby_host = emby_server.config.config.get("host")
                if self._emby_host.endswith("/"):
                    self._emby_host = self._emby_host.rstrip("/")
                if not self._emby_host.startswith("http"):
                    self._emby_host = "http://" + self._emby_host

        self.stop_service()

        if self._enabled:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("MediaWarp 服务启动中...")
            self._scheduler.add_job(
                func=self.__run_service,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ))
                + timedelta(seconds=2),
                name="MediaWarp启动服务",
            )

            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "port": self._port,
                "media_strm_path": self._media_strm_path,
                "mediaservers": self._mediaservers,
                "crx": self._crx,
                "actor_plus": self._actor_plus,
                "fanart_show": self._fanart_show,
                "external_player_url": self._external_player_url,
                "danmaku": self._danmaku,
                "video_together": self._video_together,
                "srt2ass": self._srt2ass,
            }
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """

        web_ui = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "crx",
                                    "label": "CRX美化",
                                    "hint": "crx 美化",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "actor_plus",
                                    "label": "头像过滤",
                                    "hint": "过滤没有头像的演员和制作人员",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "fanart_show",
                                    "label": "显示同人图",
                                    "hint": "显示同人图（fanart 图）",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "external_player_url",
                                    "label": "外置播放器",
                                    "hint": "是否开启外置播放器（仅 Emby）",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "danmaku",
                                    "label": "Web弹幕",
                                    "hint": "Web 弹幕",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "video_together",
                                    "label": "共同观影",
                                    "hint": "共同观影",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                ],
            },
        ]

        subtitle = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "srt2ass",
                                    "label": "SRT转ASS",
                                    "hint": "SRT 字幕转 ASS 字幕",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                ],
            },
        ]

        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "d-flex align-center"},
                        "content": [
                            {
                                "component": "VIcon",
                                "props": {
                                    "icon": "mdi-cog",
                                    "color": "primary",
                                    "class": "mr-2",
                                },
                            },
                            {"component": "span", "text": "基础设置"},
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VForm",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "enabled",
                                                            "label": "启用插件",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "port",
                                                            "label": "端口",
                                                            "hint": "反代后媒体服务器访问端口",
                                                            "persistent-hint": True,
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VSelect",
                                                        "props": {
                                                            "multiple": True,
                                                            "chips": True,
                                                            "clearable": True,
                                                            "model": "mediaservers",
                                                            "label": "媒体服务器",
                                                            "items": [
                                                                {
                                                                    "title": config.name,
                                                                    "value": config.name,
                                                                }
                                                                for config in self._mediaserver_helper.get_configs().values()
                                                                if config.type == "emby"
                                                                or config.type
                                                                == "jellyfin"
                                                            ],
                                                            "hint": "同时只能选择一个",
                                                            "persistent-hint": True,
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    },
                                ],
                            },
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 12},
                                        "content": [
                                            {
                                                "component": "VTextarea",
                                                "props": {
                                                    "model": "media_strm_path",
                                                    "label": "Emby STRM 媒体库路径",
                                                    "rows": 5,
                                                    "placeholder": "一行一个",
                                                },
                                            },
                                        ],
                                    }
                                ],
                            },
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "density": "compact",
                                    "class": "mt-2",
                                },
                                "content": [
                                    {
                                        "component": "div",
                                        "text": "注意：",
                                    },
                                    {
                                        "component": "div",
                                        "text": "如果 MoviePilot 容器为 bridge 模式需要手动映射配置的端口",
                                    },
                                    {
                                        "component": "div",
                                        "text": "更多配置可以前往 MoviePilot 配置目录找到此插件的配置目录进行详细配置文件配置",
                                    },
                                ],
                            },
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "density": "compact",
                                    "class": "mt-2",
                                },
                                "content": [
                                    {
                                        "component": "div",
                                        "text": "目前支持 115网盘STRM助手，123云盘STRM助手，CloudMediaSync，OneStrm",
                                    },
                                    {
                                        "component": "div",
                                        "text": "Symedia，q115-strm 等软件生成的STRM文件",
                                    },
                                ],
                            },
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "density": "compact",
                                    "class": "mt-2",
                                },
                                "content": [
                                    {
                                        "component": "div",
                                        "text": "感谢项目作者：https://github.com/Akimio521/MediaWarp",
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VTabs",
                        "props": {"model": "tab", "grow": True, "color": "primary"},
                        "content": [
                            {
                                "component": "VTab",
                                "props": {"value": "web-ui"},
                                "content": [
                                    {
                                        "component": "VIcon",
                                        "props": {
                                            "icon": "mdi-file-move-outline",
                                            "start": True,
                                            "color": "#1976D2",
                                        },
                                    },
                                    {"component": "span", "text": "Web页面配置"},
                                ],
                            },
                            {
                                "component": "VTab",
                                "props": {"value": "subtitle"},
                                "content": [
                                    {
                                        "component": "VIcon",
                                        "props": {
                                            "icon": "mdi-sync",
                                            "start": True,
                                            "color": "#4CAF50",
                                        },
                                    },
                                    {"component": "span", "text": "字体相关设置"},
                                ],
                            },
                        ],
                    },
                    {"component": "VDivider"},
                    {
                        "component": "VWindow",
                        "props": {"model": "tab"},
                        "content": [
                            {
                                "component": "VWindowItem",
                                "props": {"value": "web-ui"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": web_ui,
                                    }
                                ],
                            },
                            {
                                "component": "VWindowItem",
                                "props": {"value": "subtitle"},
                                "content": [
                                    {"component": "VCardText", "content": subtitle}
                                ],
                            },
                        ],
                    },
                ],
            },
        ], {
            "enabled": False,
            "port": "",
            "media_strm_path": "",
            "mediaservers": [],
            "crx": False,
            "actor_plus": False,
            "fanart_show": False,
            "external_player_url": False,
            "danmaku": False,
            "video_together": False,
            "srt2ass": False,
            "tab": "web-ui",
        }

    def get_page(self) -> List[dict]:
        pass

    def __run_service(self):
        """
        运行服务
        """
        if not Path(self.__mediawarp_path).exists():
            logger.info("尝试自动下载二级制文件中...")
            self.__download_and_extract()
            if not Path(self.__mediawarp_path).exists():
                logger.error("下载失败，MediaWarp 二级制文件不存在，无法启动插件")
                logger.info(
                    f"请将 MediaWarp 二级制文件放入 {settings.PLUGIN_DATA_PATH / self.__class__.__name__.lower()} 文件夹内"
                )
                self.__update_config()
                return

        if os.path.exists(self.__mediawarp_version_path):
            with open(self.__mediawarp_version_path, "r", encoding="utf-8") as f:
                version = f.read().strip()
            if version != self.__mediawarp_version:
                logger.info("尝试自动更新二级制文件中...")
                self.__download_and_extract()

        if not Path(self.__config_path / self.__config_filename).exists():
            logger.error("MediaWarp 配置文件不存在，无法启动插件")
            self.__update_config()
            return

        changes = {
            "Port": self._port,
            "Logger.AccessLogger.File": True,
            "Logger.AccessLogger.Console": False,
            "MediaServer.Type": "Jellyfin"
            if self._emby_server == "jellyfin"
            else "Emby",
            "MediaServer.ADDR": self._emby_host,
            "MediaServer.AUTH": self._emby_apikey,
            "Web.Index": bool(
                Path(self.__config_path / "static" / "index.html").exists()
            ),
            "Web.Crx": bool(self._crx),
            "Web.ActorPlus": bool(self._actor_plus),
            "Web.FanartShow": bool(self._fanart_show),
            "Web.Danmaku": bool(self._danmaku),
            "Web.ExternalPlayerUrl": bool(self._external_player_url),
            "Web.VideoTogether": bool(self._video_together),
            "HTTPStrm.Enable": True,
            "HTTPStrm.FinalURL": True,
            "HTTPStrm.PrefixList": self._media_strm_path.split("\n"),
            "Subtitle.SRT2ASS": bool(self._srt2ass),
        }
        self.__modify_config(Path(self.__config_path / self.__config_filename), changes)

        Path(self.__config_path).mkdir(parents=True, exist_ok=True)
        Path(self.__logs_dir).mkdir(parents=True, exist_ok=True)

        self.process = psutil.Popen([self.__mediawarp_path])

        if self.process.is_running():
            logger.info("MediaWarp 服务成功启动！")

    def __modify_config(self, config_path, modifications):
        """
        修改配置文件

        :param config_path: 配置文件路径
        :param modifications: 要修改的配置项字典
        :return: None
        """
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)

        def represent_bool(self, data):
            if data:
                return self.represent_scalar("tag:yaml.org,2002:bool", "True")
            else:
                return self.represent_scalar("tag:yaml.org,2002:bool", "False")

        RoundTripRepresenter.add_representer(bool, represent_bool)

        with open(config_path, "r", encoding="utf-8") as file:
            config = yaml.load(file)

        for key, value in modifications.items():
            keys = key.split(".")
            current = config
            for k in keys[:-1]:
                current = current.setdefault(k, {})
            current[keys[-1]] = value

        with open(config_path, "w", encoding="utf-8") as file:
            yaml.dump(config, file)

    def __get_download_url(self):
        """
        获取下载链接
        """
        base_url = "https://github.com/DDS-Derek/MediaWarp/releases/download/v{version}/MediaWarp_{version}_{os}_{arch}.tar.gz"

        machine = platform.machine().lower()
        if machine == "arm64" or machine == "aarch64":
            arch = "arm64"
        else:
            arch = "amd64"

        system = platform.system().lower()
        if system == "darwin":
            os_name = "darwin"
        else:
            os_name = "linux"

        return base_url.format(arch=arch, version=self.__mediawarp_version, os=os_name)

    def __download_and_extract(self):
        """
        下载并解压
        """
        url = self.__get_download_url()
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, "MediaWarp.tar.gz")

        try:
            Path(self.__config_path).mkdir(parents=True, exist_ok=True)

            logger.info(f"正在下载: {url}")
            response = requests.get(url, stream=True, proxies=settings.PROXY)
            response.raise_for_status()

            with open(temp_file, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info("正在解压文件...")
            with tarfile.open(temp_file, "r:gz") as tar:
                mediawarp_member = [
                    m for m in tar.getmembers() if m.name.endswith("MediaWarp")
                ]
                if mediawarp_member:
                    tar.extract(member=mediawarp_member[0], path=temp_dir)
                    extracted_path = Path(temp_dir) / mediawarp_member[0].name
                    extracted_path.chmod(0o755)
                    shutil.copy2(extracted_path, Path(self.__mediawarp_path))

                config_target = Path(self.__config_path / self.__config_filename)
                if not config_target.exists():
                    config_example_member = [
                        m
                        for m in tar.getmembers()
                        if m.name.endswith("config.yaml.example")
                    ]
                    if config_example_member:
                        tar.extract(member=config_example_member[0], path=temp_dir)
                        extracted_config = (
                            Path(temp_dir) / config_example_member[0].name
                        )
                        shutil.copy2(extracted_config, config_target)
                        logger.info(f"示例配置文件已保存到 {config_target}")

            with open(self.__mediawarp_version_path, "w", encoding="utf-8") as f:
                f.write(self.__mediawarp_version)
            logger.info(f"安装完成！MediaWarp 已安装到 {self.__mediawarp_path}")
        except Exception as e:
            logger.info(f"发生错误: {e}")
        finally:
            shutil.rmtree(temp_dir)

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
            if self.process:
                if self.process.is_running():
                    self.process.terminate()
        except Exception as e:
            logger.error(f"退出插件失败：{e}")
