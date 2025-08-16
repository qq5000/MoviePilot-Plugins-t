import concurrent.futures
from hashlib import sha1
from time import sleep
from typing import List
from urllib.parse import urlparse
from pathlib import Path

import requests
from p115client import P115Client

from app.log import logger
from app.core.metainfo import MetaInfo
from app.chain.media import MediaChain

from ..core.aliyunpan import BAligo
from ..utils.sentry import sentry_manager


@sentry_manager.capture_all_class_exceptions
class Ali2115Helper:
    """
    阿里云盘分享资源秒传 115
    """

    def __init__(self, u115_client: P115Client, aligo_client: BAligo):
        self.u115_client = u115_client
        self.ali_client = aligo_client

        self.ali_download_url = None
        self.folder_id = None
        self.file_name_list = None

    @staticmethod
    def calculate_sha1_range(url: str, start: int, length: int):
        """
        计算 sha1
        """
        end = start + length - 1
        headers = {"Range": f"bytes={start}-{end}"}
        with requests.get(url, headers=headers, stream=True) as r:
            r.raise_for_status()
            _sha1 = sha1()
            for chunk in r.iter_content(chunk_size=8192):
                _sha1.update(chunk)
            return _sha1.hexdigest().upper()

    def double_sha1_range(self, sign_check: str):
        """
        二次 sha1 校验
        """
        start_str, end_str = sign_check.split("-")
        start, end = int(start_str), int(end_str)
        length = end - start + 1
        return self.calculate_sha1_range(self.ali_download_url, start, length)

    def upload_115(
        self, file_name, file_size, m115_dir_id, full_sha1, ali_download_url
    ):
        """
        上传 115
        """
        self.ali_download_url = ali_download_url
        return self.u115_client.upload_file_init(
            filename=file_name,
            filesize=file_size,
            filesha1=full_sha1,
            pid=m115_dir_id,
            read_range_bytes_or_hash=self.double_sha1_range,
        )

    def get_ali_folder_id(self):
        """
        获取转存文件夹 ID
        """
        if self.folder_id:
            return self.folder_id
        folder_info = self.ali_client.get_folder_by_path(path="秒传转存")
        if not folder_info:
            folder_id = self.ali_client.create_folder(
                name="秒传转存", check_name_mode="overwrite"
            ).file_id
        else:
            folder_id = folder_info.file_id
        self.folder_id = folder_id
        return folder_id

    def ali_tree_share(self, share_token, rmt_mediaext: List, parent_file_id="root"):
        """
        递归分享文件
        """
        file_list = self.ali_client.get_share_file_list(
            share_token, parent_file_id=parent_file_id
        )
        for file in file_list:
            if file.type == "folder":
                yield from self.ali_tree_share(share_token, rmt_mediaext, file.file_id)
            else:
                if Path(file.name).suffix.lower() in rmt_mediaext:
                    yield file
                else:
                    logger.warn(f"【Ali2115】{file.name} 不符合媒体文件后缀，跳过秒传")

    def share_recognize_mediainfo(self, share_token):
        """
        分享转存识别媒体信息
        """
        file_list = self.ali_client.get_share_file_list(
            share_token, parent_file_id="root"
        )
        for file in file_list:
            item_name = file.name
            break
        mediachain = MediaChain()
        file_meta = MetaInfo(title=item_name)
        file_mediainfo = mediachain.recognize_by_meta(file_meta)
        return file_mediainfo

    def get_ali_download_url(self, file_id: str):
        """
        获取阿里云盘文件下载链接
        """
        return self.ali_client.get_download_url(file_id=file_id)

    def save_ali_share_to_pan(self, share_token: str):
        """
        保存分享文件到阿里云盘
        """
        self.ali_client.share_file_save_all_to_drive(
            share_token=share_token,
            to_parent_file_id=self.get_ali_folder_id(),
        )

    def get_ali_share_token(self, share_id: str):
        """
        获取分享 Token
        """
        return self.ali_client.get_share_token(share_id)

    def share_upload(self, share_token: str, parent_id: int, rmt_mediaext: List):
        """
        运行分享秒传
        """

        download_url_list: List = []
        remove_list: List = []

        def get_download_and_remove(path, info):
            """
            获取下载链接并删除
            """
            if not path and info.file_id not in remove_list:
                remove_list.append(info.file_id)

            if info.type == "file":
                if info.name not in self.file_name_list:
                    return
                url_info = self.get_ali_download_url(info.file_id)
                info_list = [
                    url_info.url,
                    url_info.size,
                    info.name,
                    str(url_info.content_hash).upper(),
                ]
                download_url_list.append(info_list)
                self.file_name_list = [
                    item for item in self.file_name_list if item != info.name
                ]

        self.save_ali_share_to_pan(share_token)
        sleep(2)
        self.file_name_list = [
            item.name for item in self.ali_tree_share(share_token, rmt_mediaext)
        ]

        while self.file_name_list:
            self.ali_client.walk_files(
                callback=get_download_and_remove,
                parent_file_id=self.get_ali_folder_id(),
            )
            sleep(3)

        self.ali_client.batch_delete_files(file_id_list=remove_list)
        logger.debug(
            f"【Ali2115】秒传文件列表: {[lst[2] for lst in download_url_list]}"
        )

        futures_map = {}
        fail_upload = 0
        success_upload = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            for url in download_url_list:
                future = executor.submit(
                    self.upload_115,
                    file_name=url[2],
                    file_size=url[1],
                    m115_dir_id=parent_id,
                    full_sha1=url[3],
                    ali_download_url=url[0],
                )
                futures_map[future] = {"url": url, "retries": 0}

            while futures_map:
                done_futures, _ = concurrent.futures.wait(
                    futures_map.keys(), return_when=concurrent.futures.FIRST_COMPLETED
                )

                for future in done_futures:
                    task_info = futures_map.pop(future)
                    url = task_info["url"]
                    retries = task_info["retries"]
                    file_name = url[2]

                    try:
                        result = future.result()
                        if (
                            result
                            and isinstance(result, dict)
                            and result.get("status") == 2
                        ):
                            logger.info(f"【Ali2115】文件 '{file_name}' 秒传成功")
                            success_upload += 1
                            continue
                        else:
                            status_code = (
                                result.get("status", "N/A")
                                if isinstance(result, dict)
                                else "N/A"
                            )
                            logger.warn(
                                f"【Ali2115】文件 '{file_name}' 上传状态异常 (status: {status_code})，准备重试..."
                            )
                    except Exception as exc:
                        logger.warn(
                            f"【Ali2115】文件 '{file_name}' 上传时发生异常: {exc}，准备重试..."
                        )

                    if retries < 3:
                        new_retries = retries + 1

                        delay = 2 * (2**retries)
                        logger.warn(
                            f"【Ali2115】文件 '{file_name}' 将在 {delay} 秒后进行第 {new_retries} 次重试..."
                        )
                        sleep(delay)
                        new_future = executor.submit(
                            self.upload_115,
                            file_name=url[2],
                            file_size=url[1],
                            m115_dir_id=parent_id,
                            full_sha1=url[3],
                            ali_download_url=url[0],
                        )
                        futures_map[new_future] = {"url": url, "retries": new_retries}
                    else:
                        logger.error(
                            f"【Ali2115】文件 '{file_name}' 已达到最大重试次数 (3)，放弃上传"
                        )
                        fail_upload += 1

        logger.info(
            f"【Ali2115】秒传文件成功 {success_upload} 个，失败 {fail_upload} 个"
        )

    @staticmethod
    def extract_share_code_from_url(url: str) -> str | None:
        """
        通过解析URL来稳定地提取阿里云盘分享码。
        """
        try:
            parsed_url = urlparse(url)
            path_parts = parsed_url.path.split("/")
            if len(path_parts) >= 3 and path_parts[-2] == "s":
                share_code = path_parts[-1]
                if share_code:
                    return share_code
        except Exception as e:
            raise e
        return None
