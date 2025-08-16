import ast
import time
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
import requests
from cachetools import cached, TTLCache
from p123client import check_response
from p123client.tool import iterdir, share_iterdir

from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfoPath
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import TransferInfo, FileItem, RefreshMediaItem, ServiceInfo
from app.core.context import MediaInfo
from app.helper.mediaserver import MediaServerHelper
from app.chain.media import MediaChain
from app.schemas.types import EventType, MediaType
from app.utils.system import SystemUtils

from .tool import P123AutoClient
class MediaInfoDownloader:
    """
    媒体信息文件下载器
    """

    def __init__(self, client: P123AutoClient, plugin_ref=None):
        self.client = client
        self.plugin_ref = plugin_ref  # 引用插件本身以便轮换账号

    @staticmethod
    def is_file_leq_1k(file_path):
        """
        判断文件是否小于 1KB
        """
        file = Path(file_path)
        if not file.exists():
            return True
        return file.stat().st_size <= 1024

    def get_download_url_with_retry(self, item: Dict, max_retry: int = None):
        """
        获取下载链接，失败则自动轮换账号池重试
        """
        plugin = self.plugin_ref
        if plugin and hasattr(plugin, '_account_pool') and plugin._account_pool:
            pool_len = len(plugin._account_pool)
        else:
            pool_len = 1
        if max_retry is None:
            max_retry = pool_len
        for attempt in range(max_retry):
            try:
                resp = self.client.download_info(
                    item,
                    base_url="",
                    headers={"User-Agent": settings.USER_AGENT},
                )
                check_response(resp)
                logger.info(f"【账号池】账号{plugin._passport if plugin else ''}获取下载地址成功")
                return resp.get("data", {}).get("DownloadUrl", None)
            except Exception as e:
                logger.warning(f"【账号池】账号{plugin._passport if plugin else ''}获取下载地址失败: {e}")
                if plugin and plugin._account_pool and max_retry > 1:
                    logger.info("【账号池】尝试轮换账号池账号后重试")
                    plugin.rotate_account()
                    self.client = plugin._client  # 更新client
                else:
                    break
        logger.error("【账号池】所有账号均尝试失败，无法获取下载地址")
        return None

    def get_download_url(self, item: Dict):
        # 兼容原有调用，直接用带重试的
        return self.get_download_url_with_retry(item)

    def save_mediainfo_file(self, file_path: Path, file_name: str, download_url: str):
        """
        保存媒体信息文件
        """
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(
            download_url,
            stream=True,
            timeout=30,
            headers={
                "User-Agent": settings.USER_AGENT,
            },
        ) as response:
            response.raise_for_status()
            with open(file_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
        logger.info(f"【媒体信息文件下载】保存 {file_name} 文件成功: {file_path}")

    def downloader(
        self,
        item: Dict,
        path: Path,
    ):
        """
        下载用户网盘文件
        """
        download_url = self.get_download_url(item=item)
        if not download_url:
            logger.error(
                f"【媒体信息文件下载】{path.name} 下载链接获取失败，无法下载该文件"
            )
            return
        self.save_mediainfo_file(
            file_path=path,
            file_name=path.name,
            download_url=download_url,
        )

    def auto_downloader(self, downloads_list: List):
        """
        根据列表自动下载
        """
        mediainfo_count: int = 0
        mediainfo_fail_count: int = 0
        mediainfo_fail_dict: List = []
        try:
            for item in downloads_list:
                if not item:
                    continue
                download_success = False
                try:
                    for _ in range(3):
                        self.downloader(item=item[0], path=Path(item[1]))
                        if not self.is_file_leq_1k(item[1]):
                            mediainfo_count += 1
                            download_success = True
                            break
                        logger.warn(
                            f"【媒体信息文件下载】{item[1]} 下载该文件失败，自动重试"
                        )
                        time.sleep(1)
                except Exception as e:
                    logger.error(f"【媒体信息文件下载】 {item[1]} 出现未知错误: {e}")
                if not download_success:
                    mediainfo_fail_count += 1
                    mediainfo_fail_dict.append(item[1])
                else:
                    continue
                if mediainfo_count % 50 == 0:
                    logger.info("【媒体信息文件下载】休眠 2s 后继续下载")
                    time.sleep(2)
        except Exception as e:
            logger.error(f"【媒体信息文件下载】出现未知错误: {e}")
        return mediainfo_count, mediainfo_fail_count, mediainfo_fail_dict


class FullSyncStrmHelper:
    """
    全量生成 STRM 文件
    """

    def __init__(
        self,
        client,
        user_rmt_mediaext: str,
        user_download_mediaext: str,
        server_address: str,
        auto_download_mediainfo: bool = False,
    ):
        self.rmt_mediaext = [
            f".{ext.strip()}" for ext in user_rmt_mediaext.replace("，", ",").split(",")
        ]
        self.download_mediaext = [
            f".{ext.strip()}"
            for ext in user_download_mediaext.replace("，", ",").split(",")
        ]
        self.auto_download_mediainfo = auto_download_mediainfo
        self.client = client
        self.strm_count = 0
        self.mediainfo_count = 0
        self.strm_fail_count = 0
        self.mediainfo_fail_count = 0
        self.strm_fail_dict: Dict[str, str] = {}
        self.mediainfo_fail_dict: List = None
        self.server_address = server_address.rstrip("/")
        self._mediainfodownloader = MediaInfoDownloader(client=self.client, plugin_ref=self)
        self._storagechain = StorageChain()
        self.download_mediainfo_list = []

    def generate_strm_files(self, full_sync_strm_paths):
        """
        生成 STRM 文件
        """
        media_paths = full_sync_strm_paths.split("\n")
        for path in media_paths:
            if not path:
                continue
            parts = path.split("#", 1)
            pan_media_dir = parts[1]
            target_dir = parts[0]

            try:
                fileitem = self._storagechain.get_file_item(
                    storage="123云盘", path=Path(pan_media_dir)
                )
                parent_id = int(fileitem.fileid)
                logger.info(f"【全量STRM生成】网盘媒体目录 ID 获取成功: {parent_id}")
            except Exception as e:
                logger.error(f"【全量STRM生成】网盘媒体目录 ID 获取失败: {e}")
                return False

            try:
                for item in iterdir(
                    self.client,
                    parent_id=parent_id,
                    interval=1,
                    max_depth=-1,
                    predicate=lambda a: not a["is_dir"],
                ):
                    file_path = pan_media_dir + "/" + item["relpath"]
                    file_path = Path(target_dir) / Path(file_path).relative_to(
                        pan_media_dir
                    )
                    file_target_dir = file_path.parent
                    file_name = file_path.stem + ".strm"
                    new_file_path = file_target_dir / file_name

                    try:
                        if self.auto_download_mediainfo:
                            if file_path.suffix in self.download_mediaext:
                                self.download_mediainfo_list.append(
                                    [
                                        {
                                            "Etag": item["Etag"],
                                            "FileID": int(item["FileId"]),
                                            "FileName": item["FileName"],
                                            "S3KeyFlag": item["S3KeyFlag"],
                                            "Size": int(item["Size"]),
                                        },
                                        str(file_path),
                                    ]
                                )
                                continue

                        if file_path.suffix not in self.rmt_mediaext:
                            logger.warn(
                                "【全量STRM生成】跳过网盘路径: %s",
                                str(file_path).replace(str(target_dir), "", 1),
                            )
                            continue

                        new_file_path.parent.mkdir(parents=True, exist_ok=True)

                        strm_url = f"{self.server_address}/api/v1/plugin/P123StrmHelper/redirect_url?apikey={settings.API_TOKEN}&name={item['FileName']}&size={item['Size']}&md5={item['Etag']}&s3_key_flag={item['S3KeyFlag']}"

                        with open(new_file_path, "w", encoding="utf-8") as file:
                            file.write(strm_url)
                        self.strm_count += 1
                        logger.info(
                            "【全量STRM生成】生成 STRM 文件成功: %s", str(new_file_path)
                        )
                    except Exception as e:
                        logger.error(
                            "【全量STRM生成】生成 STRM 文件失败: %s  %s",
                            str(new_file_path),
                            e,
                        )
                        self.strm_fail_count += 1
                        self.strm_fail_dict[str(new_file_path)] = str(e)
                        continue
            except Exception as e:
                logger.error(f"【全量STRM生成】全量生成 STRM 文件失败: {e}")
                return False
        self.mediainfo_count, self.mediainfo_fail_count, self.mediainfo_fail_dict = (
            self._mediainfodownloader.auto_downloader(
                downloads_list=self.download_mediainfo_list
            )
        )
        if self.strm_fail_dict:
            for path, error in self.strm_fail_dict.items():
                logger.warn(f"【全量STRM生成】{path} 生成错误原因: {error}")
        if self.mediainfo_fail_dict:
            for path in self.mediainfo_fail_dict:
                logger.warn(f"【全量STRM生成】{path} 下载错误")
        logger.info(
            f"【全量STRM生成】全量生成 STRM 文件完成，总共生成 {self.strm_count} 个 STRM 文件，下载 {self.mediainfo_count} 个媒体数据文件"
        )
        if self.strm_fail_count != 0 or self.mediainfo_fail_count != 0:
            logger.warn(
                f"【全量STRM生成】{self.strm_fail_count} 个 STRM 文件生成失败，{self.mediainfo_fail_count} 个媒体数据文件下载失败"
            )
        return True


class ShareStrmHelper:
    """
    根据分享生成STRM
    """

    def __init__(
        self,
        client,
        user_rmt_mediaext: str,
        user_download_mediaext: str,
        share_media_path: str,
        local_media_path: str,
        server_address: str,
        auto_download_mediainfo: bool = False,
    ):
        self.rmt_mediaext = [
            f".{ext.strip()}" for ext in user_rmt_mediaext.replace("，", ",").split(",")
        ]
        self.download_mediaext = [
            f".{ext.strip()}"
            for ext in user_download_mediaext.replace("，", ",").split(",")
        ]
        self.auto_download_mediainfo = auto_download_mediainfo
        self.client = client
        self.strm_count = 0
        self.mediainfo_count = 0
        self.strm_fail_count = 0
        self.mediainfo_fail_count = 0
        self.strm_fail_dict: Dict[str, str] = {}
        self.mediainfo_fail_dict: List = None
        self.share_media_path = share_media_path
        self.local_media_path = local_media_path
        self.server_address = server_address.rstrip("/")
        self._mediainfodownloader = MediaInfoDownloader(client=self.client, plugin_ref=self)
        self.download_mediainfo_list = []

    def has_prefix(self, full_path, prefix_path):
        """
        判断路径是否包含
        """
        full = Path(full_path).parts
        prefix = Path(prefix_path).parts

        if len(prefix) > len(full):
            return False

        return full[: len(prefix)] == prefix

    def get_share_list_creata_strm(
        self,
        parent_id: int = 0,
        share_code: str = "",
        share_pwd: str = "",
    ):
        """
        获取分享文件，生成 STRM
        """
        for item in share_iterdir(
            share_code,
            share_pwd,
            parent_id=parent_id,
            interval=1,
            max_depth=-1,
            predicate=lambda a: not a["is_dir"],
        ):
            file_path = "/" + item["relpath"]
            if not self.has_prefix(file_path, self.share_media_path):
                logger.debug(
                    "【分享STRM生成】此文件不在用户设置分享目录下，跳过网盘路径: %s",
                    str(file_path).replace(str(self.local_media_path), "", 1),
                )
                continue
            file_path = Path(self.local_media_path) / Path(file_path).relative_to(
                self.share_media_path
            )
            file_target_dir = file_path.parent
            file_name = file_path.stem + ".strm"
            new_file_path = file_target_dir / file_name

            try:
                if self.auto_download_mediainfo:
                    if file_path.suffix in self.download_mediaext:
                        self.download_mediainfo_list.append(
                            [
                                {
                                    "Etag": item["Etag"],
                                    "FileID": int(item["FileId"]),
                                    "FileName": item["FileName"],
                                    "S3KeyFlag": item["S3KeyFlag"],
                                    "Size": int(item["Size"]),
                                },
                                str(file_path),
                            ]
                        )
                        continue

                if file_path.suffix not in self.rmt_mediaext:
                    logger.warn(
                        "【分享STRM生成】文件后缀不匹配，跳过网盘路径: %s",
                        str(file_path).replace(str(self.local_media_path), "", 1),
                    )
                    continue

                new_file_path.parent.mkdir(parents=True, exist_ok=True)

                strm_url = f"{self.server_address}/api/v1/plugin/P123StrmHelper/redirect_url?apikey={settings.API_TOKEN}&name={item['FileName']}&size={item['Size']}&md5={item['Etag']}&s3_key_flag={item['S3KeyFlag']}"

                with open(new_file_path, "w", encoding="utf-8") as file:
                    file.write(strm_url)
                self.strm_count += 1
                logger.info(
                    "【分享STRM生成】生成 STRM 文件成功: %s", str(new_file_path)
                )
            except Exception as e:
                logger.error(
                    "【分享STRM生成】生成 STRM 文件失败: %s  %s",
                    str(new_file_path),
                    e,
                )
                self.strm_fail_count += 1
                self.strm_fail_dict[str(new_file_path)] = str(e)
                continue

        self.mediainfo_count, self.mediainfo_fail_count, self.mediainfo_fail_dict = (
            self._mediainfodownloader.auto_downloader(
                downloads_list=self.download_mediainfo_list
            )
        )
        if self.strm_fail_dict:
            for path, error in self.strm_fail_dict.items():
                logger.warn(f"【分享STRM生成】{path} 生成错误原因: {error}")
        if self.mediainfo_fail_dict:
            for path in self.mediainfo_fail_dict:
                logger.warn(f"【分享STRM生成】{path} 下载错误")
        logger.info(
            f"【分享STRM生成】分享生成 STRM 文件完成，总共生成 {self.strm_count} 个 STRM 文件，下载 {self.mediainfo_count} 个媒体数据文件"
        )
        if self.strm_fail_count != 0 or self.mediainfo_fail_count != 0:
            logger.warn(
                f"【分享STRM生成】{self.strm_fail_count} 个 STRM 文件生成失败，{self.mediainfo_fail_count} 个媒体数据文件下载失败"
            )


class P123StrmHelper(_PluginBase):
    # 插件名称
    plugin_name = "123云盘STRM助手"
    # 插件描述
    plugin_desc = "123云盘STRM生成一条龙服务"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/DDS-Derek/MoviePilot-Plugins/main/icons/P123Disk.png"
    # 插件版本
    plugin_version = "9.9.9"
    # 插件作者
    plugin_author = "DDSRem"
    # 作者主页
    author_url = "https://github.com/DDSRem"
    # 插件配置项ID前缀
    plugin_config_prefix = "p123strmhelper_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _client = None
    _scheduler = None
    _enabled = False
    _once_full_sync_strm = False
    _passport = None
    _password = None
    _account_pool = []
    _current_account_index = 0
    _account_pool_last_len = 0
    moviepilot_address = None
    _user_rmt_mediaext = None
    _user_download_mediaext = None
    _transfer_monitor_enabled = False
    _transfer_monitor_paths = None
    _transfer_monitor_scrape_metadata_enabled = False
    _transfer_mp_mediaserver_paths = None
    _transfer_monitor_mediaservers = None
    _transfer_monitor_media_server_refresh_enabled = False
    _timing_full_sync_strm = False
    _full_sync_auto_download_mediainfo_enabled = False
    _cron_full_sync_strm = None
    _full_sync_strm_paths = None
    _mediaservers = None
    _share_strm_enabled = False
    _share_strm_auto_download_mediainfo_enabled = False
    _user_share_code = None
    _user_share_pwd = None
    _user_share_pan_path = None
    _user_share_local_path = None
    _clear_recyclebin_enabled = False
    _clear_receive_path_enabled = False
    _cron_clear = None
    _account_pool_raw = None

    def init_plugin(self, config: dict = None):
        """
        初始化插件
        """
        if config:
            self._enabled = config.get("enabled")
            self._once_full_sync_strm = config.get("once_full_sync_strm")
            self._passport = config.get("passport")
            self._password = config.get("password")
            self.moviepilot_address = config.get("moviepilot_address")
            self._user_rmt_mediaext = config.get("user_rmt_mediaext")
            self._user_download_mediaext = config.get("user_download_mediaext")
            self._transfer_monitor_enabled = config.get("transfer_monitor_enabled")
            self._transfer_monitor_paths = config.get("transfer_monitor_paths")
            self._transfer_monitor_scrape_metadata_enabled = config.get(
                "transfer_monitor_scrape_metadata_enabled"
            )
            self._transfer_mp_mediaserver_paths = config.get(
                "transfer_mp_mediaserver_paths"
            )
            self._transfer_monitor_media_server_refresh_enabled = config.get(
                "transfer_monitor_media_server_refresh_enabled"
            )
            self._transfer_monitor_mediaservers = (
                config.get("transfer_monitor_mediaservers") or []
            )
            self._timing_full_sync_strm = config.get("timing_full_sync_strm")
            self._full_sync_auto_download_mediainfo_enabled = config.get(
                "full_sync_auto_download_mediainfo_enabled"
            )
            self._cron_full_sync_strm = config.get("cron_full_sync_strm")
            self._full_sync_strm_paths = config.get("full_sync_strm_paths")
            self._share_strm_enabled = config.get("share_strm_enabled")
            self._share_strm_auto_download_mediainfo_enabled = config.get(
                "share_strm_auto_download_mediainfo_enabled"
            )
            self._user_share_code = config.get("user_share_code")
            self._user_share_pwd = config.get("user_share_pwd")
            self._user_share_pan_path = config.get("user_share_pan_path")
            self._user_share_local_path = config.get("user_share_local_path")
            self._clear_recyclebin_enabled = config.get("clear_recyclebin_enabled")
            self._clear_receive_path_enabled = config.get("clear_receive_path_enabled")
            self._cron_clear = config.get("cron_clear")
            self._account_pool_raw = config.get("account_pool", "")
            self._account_pool = []
            for line in self._account_pool_raw.strip().splitlines():
                if "#" in line:
                    passport, password = line.split("#", 1)
                    self._account_pool.append({"passport": passport.strip(), "password": password.strip()})
            self._account_pool_last_len = len(self._account_pool)
            if not self._user_rmt_mediaext:
                self._user_rmt_mediaext = "mp4,mkv,ts,iso,rmvb,avi,mov,mpeg,mpg,wmv,3gp,asf,m4v,flv,m2ts,tp,f4v"
            if not self._user_download_mediaext:
                self._user_download_mediaext = "srt,ssa,ass"
            if not self._cron_full_sync_strm:
                self._cron_full_sync_strm = "0 */7 * * *"
            if not self._cron_clear:
                self._cron_clear = "0 */7 * * *"
            if not self._user_share_pan_path:
                self._user_share_pan_path = "/"
            self.__update_config()

        # 启动时账号池赋值到单账号配置
        if self._account_pool:
            self._current_account_index = 0
            account = self._account_pool[self._current_account_index]
            self._passport = account.get("passport", "")
            self._password = account.get("password", "")
            passport_masked = self._passport
            if len(passport_masked) >= 7:
                passport_masked = f"{passport_masked[:3]}****{passport_masked[-4:]}"
            logger.info(f"【账号池】账号初始化：当前索引 1/{len(self._account_pool)}，账号：{passport_masked}")

        try:
            self._client = P123AutoClient(self._passport, self._password)
        except Exception as e:
            logger.error(f"123云盘客户端创建失败: {e}")

        self.stop_service()

        # 定时账号池轮换（只要有账号池）
        if self._enabled and len(self._account_pool) > 1:
            if not self._scheduler:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.rotate_account,
                trigger='cron',
                minute='0,30',
                name="账号池轮换"
            )
            # 新增：每分钟输出倒计时日志
            self._scheduler.add_job(
                func=self.log_rotate_countdown,
                trigger='cron',
                minute='*',
                name="账号池轮换倒计时"
            )
            # 新增：每天0点清理日志
            self._scheduler.add_job(
                func=self.clean_logs,
                trigger='cron',
                hour=0,
                minute=0,
                name="日志清理"
            )
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

        if self._enabled and self._once_full_sync_strm:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.full_sync_strm_files,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ))
                + timedelta(seconds=3),
                name="123云盘助手立刻全量同步",
            )
            self._once_full_sync_strm = False
            self.__update_config()
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

        if self._enabled and self._share_strm_enabled:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.share_strm_files,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ))
                + timedelta(seconds=3),
                name="123云盘助手分享生成STRM",
            )
            self._share_strm_enabled = False
            self.__update_config()
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def rotate_account(self):
        if not self._account_pool:
            return
        old_index = self._current_account_index
        self._current_account_index = (self._current_account_index + 1) % len(self._account_pool)
        account = self._account_pool[self._current_account_index]
        self._passport = account.get("passport", "")
        self._password = account.get("password", "")
        passport_masked = self._passport
        if len(passport_masked) >= 7:
            passport_masked = f"{passport_masked[:3]}****{passport_masked[-4:]}"
        logger.info(f"【账号池】账号轮换：切换到 {self._current_account_index+1}/{len(self._account_pool)}（{passport_masked}）")
        try:
            self._client = P123AutoClient(self._passport, self._password)
            logger.info(f"【账号池】已切换账号并重建 client：{passport_masked}")
        except Exception as e:
            logger.error(f"【账号池】切换账号时重建 client 失败: {e}")
        self.__update_config()

    def log_rotate_countdown(self):
        try:
            logger.info("log_rotate_countdown called")  # 调试用
            from datetime import datetime, timedelta
            import pytz
            now = datetime.now(pytz.timezone(settings.TZ))
            # 计算下一个半点
            next_half_hour = now.replace(second=0, microsecond=0)
            if now.minute < 30:
                next_half_hour = next_half_hour.replace(minute=30)
            else:
                # 到下一个小时的30分
                next_half_hour = (next_half_hour + timedelta(hours=1)).replace(minute=30)
            minutes_left = int((next_half_hour - now).total_seconds() // 60)
            logger.info(f"【账号池】距离下次账号自动轮换还有 {minutes_left} 分钟")
        except Exception as e:
            logger.error(f"log_rotate_countdown error: {e}")

    def get_state(self) -> bool:
        return self._enabled

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        """
        服务信息
        """
        _mediaserver_helper = MediaServerHelper()

        if not self._transfer_monitor_mediaservers:
            logger.warning("尚未配置媒体服务器，请检查配置")
            return None

        services = _mediaserver_helper.get_services(
            name_filters=self._transfer_monitor_mediaservers
        )
        if not services:
            logger.warning("获取媒体服务器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"媒体服务器 {service_name} 未连接，请检查配置")
            else:
                active_services[service_name] = service_info

        if not active_services:
            logger.warning("没有已连接的媒体服务器，请检查配置")
            return None

        return active_services

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        BASE_URL: {server_url}/api/v1/plugin/P123StrmHelper/redirect_url?apikey={APIKEY}
        0. 查询带 s3_key_flag
            url: ${BASE_URL}&name={name}&size={size}&md5={md5}&s3_key_flag={s3_key_flag}
        1. 查询不带 s3_key_flag
           会尝试先秒传到你的网盘的 "/我的秒传" 目录下，名字为 f"{md5}-{size}" 的文件，然后再获取下载链接
            url: ${BASE_URL}&name={name}&size={size}&md5={md5}
        """
        return [
            {
                "path": "/redirect_url",
                "endpoint": self.redirect_url,
                "methods": ["GET", "POST", "HEAD"],
                "summary": "302跳转",
                "description": "123云盘302跳转",
            },
            {
                "path": "/rotate_account",
                "endpoint": self.api_rotate_account,
                "methods": ["POST"],
                "summary": "手动切换账号池账号",
                "description": "手动切换账号池账号，切换后自动重建client。",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        cron_service = []
        if (
            self._cron_full_sync_strm
            and self._timing_full_sync_strm
            and self._full_sync_strm_paths
        ):
            cron_service.append(
                {
                    "id": "P123StrmHelper_full_sync_strm_files",
                    "name": "定期全量同步123媒体库",
                    "trigger": CronTrigger.from_crontab(self._cron_full_sync_strm),
                    "func": self.full_sync_strm_files,
                    "kwargs": {},
                }
            )
        if self._cron_clear and (
            self._clear_recyclebin_enabled or self._clear_receive_path_enabled
        ):
            cron_service.append(
                {
                    "id": "P123StrmHelper_main_cleaner",
                    "name": "定期清理123空间",
                    "trigger": CronTrigger.from_crontab(self._cron_clear),
                    "func": self.main_cleaner,
                    "kwargs": {},
                }
            )
        if cron_service:
            return cron_service

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        _mediaserver_helper = MediaServerHelper()
        # ... 其余 tab 定义 ...

        # 基础设置卡片内容
        base_card_content = [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
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
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "passport",
                                    "label": "手机号",
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "password",
                                    "label": "密码",
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "moviepilot_address",
                                    "label": "MoviePilot 内网访问地址",
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
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "user_rmt_mediaext",
                                    "label": "可整理媒体文件扩展名",
                                },
                            }
                        ],
                    }
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
                                "component": "VTextField",
                                "props": {
                                    "model": "user_download_mediaext",
                                    "label": "可下载媒体数据文件扩展名",
                                },
                            }
                        ],
                    }
                ],
            },
            # 新增手动切换账号按钮
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "block": True,
                                    "class": "mt-2",
                                    "action": "rotate_account",
                                },
                                "content": "手动切换账号池账号"
                            }
                        ],
                    }
                ],
            },
        ]

        # 账号池独立卡片
        account_pool_card = {
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
                                "icon": "mdi-account-switch",
                                "color": "primary",
                                "class": "mr-2",
                            },
                        },
                        {"component": "span", "text": "账号池设置"},
                    ],
                },
                {"component": "VDivider"},
                {
                    "component": "VCardText",
                    "content": [
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
                                                "model": "account_pool",
                                                "label": "账号池（每行一个，格式：手机号#密码）",
                                                "rows": 5,
                                                "placeholder": "例如：\n13800000000#password1\n13900000000#password2",
                                                "hint": "支持多个账号，自动轮换，每小时切换一次。",
                                                "persistent-hint": True,
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        }

        # 拼接当前账号池内容用于回显
        account_pool_str = "\n".join([
            f"{acc['passport']}#{acc['password']}" for acc in self._account_pool
        ])

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
                    {"component": "VCardText", "content": base_card_content},
                ],
            },
            account_pool_card,
            # ... 其余卡片 ...
        ], {
            "enabled": False,
            "once_full_sync_strm": False,
            "passport": "",
            "password": "",
            "moviepilot_address": "",
            "user_rmt_mediaext": "mp4,mkv,ts,iso,rmvb,avi,mov,mpeg,mpg,wmv,3gp,asf,m4v,flv,m2ts,tp,f4v",
            "user_download_mediaext": "srt,ssa,ass",
            "account_pool": self._account_pool_raw,
            # ... 其余默认配置 ...
        }

    def get_page(self) -> List[dict]:
        pass

    def __update_config(self):
        # 账号池原始字符串直接保存
        self.update_config({
            "enabled": self._enabled,
            "once_full_sync_strm": self._once_full_sync_strm,
            "passport": self._passport,
            "password": self._password,
            "moviepilot_address": self.moviepilot_address,
            "user_rmt_mediaext": self._user_rmt_mediaext,
            "user_download_mediaext": self._user_download_mediaext,
            "transfer_monitor_enabled": self._transfer_monitor_enabled,
            "transfer_monitor_paths": self._transfer_monitor_paths,
            "transfer_monitor_scrape_metadata_enabled": self._transfer_monitor_scrape_metadata_enabled,
            "transfer_mp_mediaserver_paths": self._transfer_mp_mediaserver_paths,
            "transfer_monitor_media_server_refresh_enabled": self._transfer_monitor_media_server_refresh_enabled,
            "transfer_monitor_mediaservers": self._transfer_monitor_mediaservers,
            "timing_full_sync_strm": self._timing_full_sync_strm,
            "full_sync_auto_download_mediainfo_enabled": self._full_sync_auto_download_mediainfo_enabled,
            "cron_full_sync_strm": self._cron_full_sync_strm,
            "full_sync_strm_paths": self._full_sync_strm_paths,
            "share_strm_enabled": self._share_strm_enabled,
            "share_strm_auto_download_mediainfo_enabled": self._share_strm_auto_download_mediainfo_enabled,
            "user_share_code": self._user_share_code,
            "user_share_pwd": self._user_share_pwd,
            "user_share_pan_path": self._user_share_pan_path,
            "user_share_local_path": self._user_share_local_path,
            "clear_recyclebin_enabled": self._clear_recyclebin_enabled,
            "clear_receive_path_enabled": self._clear_receive_path_enabled,
            "cron_clear": self._cron_clear,
            "account_pool": self._account_pool_raw,
        })

    @staticmethod
    def has_prefix(full_path, prefix_path):
        """
        判断路径是否包含
        """
        full = Path(full_path).parts
        prefix = Path(prefix_path).parts

        if len(prefix) > len(full):
            return False

        return full[: len(prefix)] == prefix

    def __get_media_path(self, paths, media_path):
        """
        获取媒体目录路径
        """
        media_paths = paths.split("\n")
        for path in media_paths:
            if not path:
                continue
            parts = path.split("#", 1)
            if self.has_prefix(media_path, parts[1]):
                return True, parts[0], parts[1]
        return False, None, None

    @staticmethod
    def media_scrape_metadata(
        path,
        item_name: str = "",
        mediainfo: MediaInfo = None,
        meta: MetaBase = None,
    ):
        """
        媒体刮削服务
        :param path: 媒体文件路径
        :param item_name: 媒体名称
        :param meta: 元数据
        :param mediainfo: 媒体信息
        """
        item_name = item_name if item_name else Path(path).name
        mediachain = MediaChain()
        logger.info(f"【媒体刮削】{item_name} 开始刮削元数据")
        if mediainfo:
            # 整理文件刮削
            if mediainfo.type == MediaType.MOVIE:
                # 电影刮削上级文件夹
                dir_path = Path(path).parent
                fileitem = FileItem(
                    storage="local",
                    type="dir",
                    path=str(dir_path),
                    name=dir_path.name,
                    basename=dir_path.stem,
                    modify_time=dir_path.stat().st_mtime,
                )
            else:
                # 电视剧刮削文件夹
                # 通过重命名格式判断根目录文件夹
                # 计算重命名中的文件夹层数
                rename_format_level = len(settings.TV_RENAME_FORMAT.split("/")) - 1
                if rename_format_level < 1:
                    file_path = Path(path)
                    fileitem = FileItem(
                        storage="local",
                        type="file",
                        path=str(file_path).replace("\\", "/"),
                        name=file_path.name,
                        basename=file_path.stem,
                        extension=file_path.suffix[1:],
                        size=file_path.stat().st_size,
                        modify_time=file_path.stat().st_mtime,
                    )
                else:
                    dir_path = Path(Path(path).parents[rename_format_level - 1])
                    fileitem = FileItem(
                        storage="local",
                        type="dir",
                        path=str(dir_path),
                        name=dir_path.name,
                        basename=dir_path.stem,
                        modify_time=dir_path.stat().st_mtime,
                    )
            mediachain.scrape_metadata(
                fileitem=fileitem, meta=meta, mediainfo=mediainfo
            )
        else:
            # 对于没有 mediainfo 的媒体文件刮削
            # 获取媒体信息
            meta = MetaInfoPath(Path(path))
            mediainfo = mediachain.recognize_by_meta(meta)
            # 判断刮削路径
            # 先获取上级目录 meta
            file_type = "dir"
            dir_path = Path(path).parent
            tem_mediainfo = mediachain.recognize_by_meta(MetaInfoPath(dir_path))
            # 只有上级目录信息和文件的信息一致时才继续判断上级目录
            if tem_mediainfo and tem_mediainfo.imdb_id == mediainfo.imdb_id:
                if mediainfo.type == MediaType.TV:
                    # 如果是电视剧，再次获取上级目录媒体信息，兼容电视剧命名，获取 mediainfo
                    dir_path = dir_path.parent
                    tem_mediainfo = mediachain.recognize_by_meta(MetaInfoPath(dir_path))
                    if tem_mediainfo and tem_mediainfo.imdb_id == mediainfo.imdb_id:
                        # 存在 mediainfo 则使用本级目录
                        finish_path = dir_path
                    else:
                        # 否则使用上级目录
                        logger.warn(f"【媒体刮削】{dir_path} 无法识别文件媒体信息！")
                        finish_path = Path(path).parent
                else:
                    # 电影情况，使用当前目录和元数据
                    finish_path = dir_path
            else:
                # 如果上级目录没有媒体信息则使用传入的路径
                logger.warn(f"【媒体刮削】{dir_path} 无法识别文件媒体信息！")
                finish_path = Path(path)
                file_type = "file"
            fileitem = FileItem(
                storage="local",
                type=file_type,
                path=str(finish_path),
                name=finish_path.name,
                basename=finish_path.stem,
                modify_time=finish_path.stat().st_mtime,
            )
            mediachain.scrape_metadata(
                fileitem=fileitem, meta=meta, mediainfo=mediainfo
            )

        logger.info(f"【媒体刮削】{item_name} 刮削元数据完成")

    @cached(cache=TTLCache(maxsize=1, ttl=2 * 60))
    def redirect_url(
        self,
        request: Request,
        name: str = "",
        size: int = 0,
        md5: str = "",
        s3_key_flag: str = "",
    ):
        """
        123云盘302跳转
        """
        if not s3_key_flag:
            try:
                resp = self._client.fs_mkdir("我的秒传")
                check_response(resp)
                resp = self._client.upload_file_fast(
                    file_md5=md5,
                    file_name=f"{md5}-{size}",
                    file_size=size,
                    parent_id=resp["data"]["Info"]["FileId"],
                    duplicate=2,
                )
                check_response(resp)
                payload = resp["data"]["Info"]
                logger.info(
                    f"【302跳转服务】转存 {name} 文件成功: {payload['S3KeyFlag']}"
                )
            except Exception as e:
                logger.error(f"【302跳转服务】转存 {name} 文件失败: {e}")
                return JSONResponse(
                    {"state": False, "message": f"转存 {name} 文件失败: {e}"}, 500
                )
        else:
            payload = {
                "S3KeyFlag": s3_key_flag,
                "FileName": name,
                "Etag": md5,
                "Size": size,
            }

        try:
            user_agent = request.headers.get("User-Agent") or b""
            logger.debug(f"【302跳转服务】获取到客户端UA: {user_agent}")
            resp = self._client.download_info(
                payload,
                base_url="",
                headers={"User-Agent": user_agent},
            )
            check_response(resp)
            url = resp["data"]["DownloadUrl"]
            logger.info(f"【302跳转服务】获取 123 下载地址成功: {url}")
        except Exception as e:
            logger.error(f"【302跳转服务】获取 123 下载地址失败: {e}")

        return RedirectResponse(url, 302)

    @eventmanager.register(EventType.TransferComplete)
    def generate_strm(self, event: Event):
        """
        监控目录整理生成 STRM 文件
        """

        def generate_strm_files(
            target_dir: Path,
            pan_media_dir: Path,
            item_dest_path: Path,
            basename: str,
            url: str,
        ):
            """
            依据网盘路径生成 STRM 文件
            """
            try:
                pan_media_dir = str(Path(pan_media_dir))
                pan_path = Path(item_dest_path).parent
                pan_path = str(Path(pan_path))
                if self.has_prefix(pan_path, pan_media_dir):
                    pan_path = pan_path[len(pan_media_dir) :].lstrip("/").lstrip("\\")
                file_path = Path(target_dir) / pan_path
                file_name = basename + ".strm"
                new_file_path = file_path / file_name
                new_file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(new_file_path, "w", encoding="utf-8") as file:
                    file.write(url)
                logger.info(
                    "【监控整理STRM生成】生成 STRM 文件成功: %s", str(new_file_path)
                )
                return True, new_file_path
            except Exception as e:  # noqa: F841
                logger.error(
                    "【监控整理STRM生成】生成 %s 文件失败: %s", str(new_file_path), e
                )
                return False, None

        if (
            not self._enabled
            or not self._transfer_monitor_enabled
            or not self._transfer_monitor_paths
            or not self.moviepilot_address
        ):
            return

        item = event.event_data
        if not item:
            return

        # 转移信息
        item_transfer: TransferInfo = item.get("transferinfo")
        # 媒体信息
        mediainfo: MediaInfo = item.get("mediainfo")
        # 元数据信息
        meta: MetaBase = item.get("meta")

        item_dest_storage: FileItem = item_transfer.target_item.storage
        if item_dest_storage != "123云盘":
            return

        # 网盘目的地目录
        itemdir_dest_path: FileItem = item_transfer.target_diritem.path
        # 网盘目的地路径（包含文件名称）
        item_dest_path: FileItem = item_transfer.target_item.path
        # 网盘目的地文件名称
        item_dest_name: FileItem = item_transfer.target_item.name
        # 网盘目的地文件名称（不包含后缀）
        item_dest_basename: FileItem = item_transfer.target_item.basename
        # 网盘目的地文件网盘详细信息
        item_dest_pickcode: FileItem = item_transfer.target_item.pickcode
        if not item_dest_pickcode:
            logger.error(
                f"【监控整理STRM生成】{item_dest_name} 不存在网盘详细信息，无法生成 STRM 文件"
            )
            return
        item_dest_info = ast.literal_eval(item_dest_pickcode)
        # 是否蓝光原盘
        item_bluray = SystemUtils.is_bluray_dir(Path(itemdir_dest_path))
        # 目标字幕文件清单
        subtitle_list = getattr(item_transfer, "subtitle_list_new", [])
        # 目标音频文件清单
        audio_list = getattr(item_transfer, "audio_list_new", [])

        __itemdir_dest_path, local_media_dir, pan_media_dir = self.__get_media_path(
            self._transfer_monitor_paths, itemdir_dest_path
        )
        if not __itemdir_dest_path:
            logger.debug(
                f"【监控整理STRM生成】{item_dest_name} 路径匹配不符合，跳过整理"
            )
            return
        logger.debug("【监控整理STRM生成】匹配到网盘文件夹路径: %s", str(pan_media_dir))

        if item_bluray:
            logger.warning(
                f"【监控整理STRM生成】{item_dest_name} 为蓝光原盘，不支持生成 STRM 文件: {item_dest_path}"
            )
            return

        if (
            not item_dest_info["FileName"]
            or not item_dest_info["Size"]
            or not item_dest_info["Etag"]
            or not item_dest_info["S3KeyFlag"]
        ):
            logger.error(
                f"【监控整理STRM生成】{item_dest_name} 缺失必要文件信息，无法生成 STRM 文件: {item_dest_info}"
            )
            return

        strm_url = f"{self.moviepilot_address.rstrip('/')}/api/v1/plugin/P123StrmHelper/redirect_url?apikey={settings.API_TOKEN}&name={item_dest_info['FileName']}&size={item_dest_info['Size']}&md5={item_dest_info['Etag']}&s3_key_flag={item_dest_info['S3KeyFlag']}"

        status, strm_target_path = generate_strm_files(
            target_dir=local_media_dir,
            pan_media_dir=pan_media_dir,
            item_dest_path=item_dest_path,
            basename=item_dest_basename,
            url=strm_url,
        )
        if not status:
            return

        try:
            _storagechain = StorageChain()
            _mediainfodownloader = MediaInfoDownloader(client=self._client, plugin_ref=self)

            if subtitle_list:
                logger.info("【监控整理STRM生成】开始下载字幕文件")
                for _path in subtitle_list:
                    fileitem = _storagechain.get_file_item(
                        storage="123云盘", path=Path(_path)
                    )
                    fileitem_info = ast.literal_eval(fileitem.pickcode)
                    download_url = _mediainfodownloader.get_download_url(
                        item={
                            "Etag": fileitem_info["Etag"],
                            "FileID": int(fileitem_info["FileId"]),
                            "FileName": fileitem_info["FileName"],
                            "S3KeyFlag": fileitem_info["S3KeyFlag"],
                            "Size": int(fileitem_info["Size"]),
                        }
                    )
                    if not download_url:
                        logger.error(
                            f"【监控整理STRM生成】{Path(_path).name} 下载链接获取失败，无法下载该文件"
                        )
                        continue
                    _file_path = Path(local_media_dir) / Path(_path).relative_to(
                        pan_media_dir
                    )
                    _mediainfodownloader.save_mediainfo_file(
                        file_path=Path(_file_path),
                        file_name=_file_path.name,
                        download_url=download_url,
                    )

            if audio_list:
                logger.info("【监控整理STRM生成】开始下载音频文件")
                for _path in audio_list:
                    fileitem = _storagechain.get_file_item(
                        storage="123云盘", path=Path(_path)
                    )
                    fileitem_info = ast.literal_eval(fileitem.pickcode)
                    download_url = _mediainfodownloader.get_download_url(
                        item={
                            "Etag": fileitem_info["Etag"],
                            "FileID": int(fileitem_info["FileId"]),
                            "FileName": fileitem_info["FileName"],
                            "S3KeyFlag": fileitem_info["S3KeyFlag"],
                            "Size": int(fileitem_info["Size"]),
                        }
                    )
                    if not download_url:
                        logger.error(
                            f"【监控整理STRM生成】{Path(_path).name} 下载链接获取失败，无法下载该文件"
                        )
                        continue
                    _file_path = Path(local_media_dir) / Path(_path).relative_to(
                        pan_media_dir
                    )
                    _mediainfodownloader.save_mediainfo_file(
                        file_path=Path(_file_path),
                        file_name=_file_path.name,
                        download_url=download_url,
                    )
        except Exception as e:
            logger.error(f"【监控整理STRM生成】媒体信息文件下载出现未知错误: {e}")

        if self._transfer_monitor_scrape_metadata_enabled:
            self.media_scrape_metadata(
                path=strm_target_path,
                item_name=item_dest_name,
                mediainfo=mediainfo,
                meta=meta,
            )

        if self._transfer_monitor_media_server_refresh_enabled:
            if not self.service_infos:
                return

            logger.info("【监控整理STRM生成】开始刷新媒体服务器")

            if self._transfer_mp_mediaserver_paths:
                status, mediaserver_path, moviepilot_path = self.__get_media_path(
                    self._transfer_mp_mediaserver_paths, strm_target_path
                )
                if status:
                    logger.info("【监控整理STRM生成】刷新媒体服务器目录替换中...")
                    strm_target_path = strm_target_path.replace(
                        moviepilot_path, mediaserver_path
                    ).replace("\\", "/")
                    logger.info(
                        f"【监控整理STRM生成】刷新媒体服务器目录替换: {moviepilot_path} --> {mediaserver_path}"
                    )
                    logger.info(
                        f"【监控整理STRM生成】刷新媒体服务器目录: {strm_target_path}"
                    )

            items = [
                RefreshMediaItem(
                    title=mediainfo.title,
                    year=mediainfo.year,
                    type=mediainfo.type,
                    category=mediainfo.category,
                    target_path=Path(strm_target_path),
                )
            ]

            for name, service in self.service_infos.items():
                if hasattr(service.instance, "refresh_library_by_items"):
                    service.instance.refresh_library_by_items(items)
                elif hasattr(service.instance, "refresh_root_library"):
                    service.instance.refresh_root_library()
                else:
                    logger.warning(f"【监控整理STRM生成】{name} 不支持刷新")

    def full_sync_strm_files(self):
        """
        全量同步
        """
        if not self._full_sync_strm_paths or not self.moviepilot_address:
            return

        strm_helper = FullSyncStrmHelper(
            user_rmt_mediaext=self._user_rmt_mediaext,
            user_download_mediaext=self._user_download_mediaext,
            auto_download_mediainfo=self._full_sync_auto_download_mediainfo_enabled,
            client=self._client,
            server_address=self.moviepilot_address,
        )
        strm_helper.generate_strm_files(
            full_sync_strm_paths=self._full_sync_strm_paths,
        )

    def share_strm_files(self):
        """
        分享生成STRM
        """
        if (
            not self._user_share_pan_path
            or not self._user_share_local_path
            or not self.moviepilot_address
        ):
            return

        if not self._user_share_code:
            return
        share_code = self._user_share_code
        share_pwd = self._user_share_pwd

        try:
            strm_helper = ShareStrmHelper(
                user_rmt_mediaext=self._user_rmt_mediaext,
                user_download_mediaext=self._user_download_mediaext,
                auto_download_mediainfo=self._share_strm_auto_download_mediainfo_enabled,
                client=self._client,
                server_address=self.moviepilot_address,
                share_media_path=self._user_share_pan_path,
                local_media_path=self._user_share_local_path,
            )
            strm_helper.get_share_list_creata_strm(
                parent_id=0,
                share_code=share_code,
                share_pwd=share_pwd,
            )
        except Exception as e:
            logger.error(f"【分享STRM生成】运行失败: {e}")
            return

    def main_cleaner(self):
        """
        主清理模块
        """
        if self._clear_receive_path_enabled:
            self.clear_receive_path()

        time.sleep(2)

        if self._clear_recyclebin_enabled:
            self.clear_recyclebin()

    def clear_recyclebin(self):
        """
        清空回收站
        """
        try:
            logger.info("【回收站清理】开始清理回收站")
            resp = self._client.fs_trash_clear()
            if resp["code"] == 7301:
                logger.info("【回收站清理】回收站已清空")
            else:
                logger.error(f"【回收站清理】清理回收站运行失败: {resp}")
        except Exception as e:
            logger.error(f"【回收站清理】清理回收站运行失败: {e}")
            return

    def clear_receive_path(self):
        """
        清空我的秒传
        """
        try:
            logger.info("【我的秒传清理】开始清理我的秒传")
            _storagechain = StorageChain()
            fileitem = _storagechain.get_file_item(
                storage="123云盘", path=Path("/我的秒传")
            )
            if not fileitem:
                logger.info("【我的秒传清理】我的秒传目录为空，无需清理")
                return
            parent_id = int(fileitem.fileid)
            logger.info(f"【我的秒传清理】我的秒传目录 ID 获取成功: {parent_id}")
            resp = self._client.fs_trash(parent_id, event="intoRecycle")
            check_response(resp)
            logger.info("【我的秒传清理】我的秒传已清空")
        except Exception as e:
            logger.error(f"【我的秒传清理】清理我的秒传运行失败: {e}")
            return

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
        except Exception as e:
            print(str(e))

    def api_rotate_account(self, request: Request):
        """
        手动切换账号池账号API
        """
        self.rotate_account()
        return JSONResponse({"state": True, "message": "账号已切换"})

    def clean_logs(self):
        try:
            # 这里可以根据实际日志路径进行清理，如删除日志文件或只输出提示
            # 示例：仅输出日志清理提示
            logger.info("【日志清理】每天0点定时清理日志（请根据实际需求实现日志文件删除）")
        except Exception as e:
            logger.error(f"【日志清理】清理日志时出错: {e}")
