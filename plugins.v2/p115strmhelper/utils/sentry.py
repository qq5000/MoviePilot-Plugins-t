import functools
import inspect
import base64

import sentry_sdk
from sentry_sdk.hub import Hub
from sentry_sdk.client import Client
from sentry_sdk.integrations.stdlib import StdlibIntegration
from sentry_sdk.integrations.excepthook import ExcepthookIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.dedupe import DedupeIntegration

from app.log import logger

from ..core.config import configer


class NoopSentryHub(Hub):
    """
    一个不执行任何操作的 Sentry Hub，用于禁用状态
    """

    def capture_event(self, event, hint=None, scope=None):
        pass

    def capture_exception(self, error=None, **kwargs):
        pass

    def capture_message(self, message, **kwargs):
        pass

    def configure_scope(self, callback=None):
        class NoopScope:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        if callback:
            callback(NoopScope())
        return NoopScope()

    def add_breadcrumb(self, crumb=None, hint=None, **kwargs):
        pass

    def push_scope(self, callback=None):
        class NoopContextManager:
            def __enter__(self):
                pass

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

        return NoopContextManager()

    def pop_scope(self, *args, **kwargs):
        pass

    def flush(self, timeout=None, callback=None):
        pass

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class SentryManager:
    """
    Sentry 状态和行为的管理器
    """

    def __init__(self):
        self.sentry_hub = NoopSentryHub()
        self._patched = False
        self.reload_config()

    def reload_config(self):
        """
        根据配置文件动态开启或关闭 Sentry 功能
        """
        is_enabled = configer.get_config("error_info_upload") is not False
        is_real_hub_active = isinstance(self.sentry_hub, Hub) and not isinstance(
            self.sentry_hub, NoopSentryHub
        )

        if is_enabled:
            if is_real_hub_active:
                logger.debug("【Sentry】Sentry is already enabled. No changes made.")
                return

            logger.debug("【Sentry】Enabling Sentry error reporting...")
            self.sentry_hub = Hub(
                Client(
                    dsn=base64.b64decode(
                        "aHR0cHM6Ly82YTk0ZjI2N2NjOTY0Y2ZiOTk5ZjQyNDgwNGIyMTE1M0BnbGl0Y2h0aXAuZGRzcmVtLmNvbS80"
                    ).decode("utf-8"),
                    release="p115strmhelper@v2.0.15",
                    default_integrations=False,
                    integrations=[
                        DedupeIntegration(),
                        StdlibIntegration(),
                        ExcepthookIntegration(always_run=True),
                        SqlalchemyIntegration(),
                    ],
                )
            )

            if not self._patched:
                self._apply_monkey_patch()
                self._patched = True

        else:
            if not is_real_hub_active:
                logger.debug("【Sentry】Sentry is already disabled. No changes made.")
                return

            logger.debug("【Sentry】Disabling Sentry error reporting...")
            self.sentry_hub = NoopSentryHub()

    def _apply_monkey_patch(self):
        """
        应用猴子补丁，确保只对我们自己的 Hub 上报
        """
        _original_capture_exception = sentry_sdk.capture_exception

        def _patched_capture_exception(*args, **kwargs):
            if Hub.current is self.sentry_hub:
                _original_capture_exception(*args, **kwargs)

        sentry_sdk.capture_exception = _patched_capture_exception

    def capture_plugin_exceptions(self, func):
        """
        作为方法使用的函数装饰器
        """
        if getattr(func, "_sentry_captured", False):
            return func

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with self.sentry_hub:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    with self.sentry_hub.configure_scope() as scope:
                        scope.set_tag("capture_source", "plugin_decorator")
                        scope.set_tag("function_name", func.__name__)
                    self.sentry_hub.capture_exception(e)
                    raise

        wrapper._sentry_captured = True  # pylint: disable=W0212,protected-access
        return wrapper

    def capture_all_class_exceptions(self, cls):
        """
        作为方法使用的类装饰器
        """
        for name, attr in cls.__dict__.items():
            if name.startswith("_"):
                continue

            original_function, is_static, is_class = None, False, False

            if isinstance(attr, staticmethod):
                original_function, is_static = attr.__func__, True
            elif isinstance(attr, classmethod):
                original_function, is_class = attr.__func__, True
            elif inspect.isfunction(attr):
                original_function = attr

            if original_function:
                wrapped_function = self.capture_plugin_exceptions(original_function)

                if is_static:
                    final_method = staticmethod(wrapped_function)
                elif is_class:
                    final_method = classmethod(wrapped_function)
                else:
                    final_method = wrapped_function

                setattr(cls, name, final_method)
        return cls


sentry_manager = SentryManager()
