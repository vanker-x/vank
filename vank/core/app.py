import atexit
import inspect
import logging
import typing as t
from vank.core.config import conf
from vank.core.views.view import View
from vank.utils import import_from_str
from vank.__version__ import __version__
from vank.core.routing.route import Route
from vank.core.http.request import Request
from vank.core.routing.router import Router
from wsgiref.simple_server import make_server
from vank.utils.reloader import run_in_reloader
from vank.middleware.base import BaseMiddleware
from vank.core.http.response import BaseResponse
from vank.core.context import request, application
from vank.core.views.static.views import StaticView
from vank.core.exceptions import NonResponseException
from vank.utils.log import setup_config as setup_log_config
from vank.core.handlers.exception import conv_exc_to_response
from vank.utils.signal import on_request_start, on_request_end, on_stop_down

logger = logging.getLogger('console')


class Base:
    def __init__(self):
        # 实例化路由映射表
        self.router = Router()

    def __set_route(self, route_path, view_func, methods, **kwargs):
        """
        添加路由
        """
        assert route_path.startswith('/'), f'{view_func} 视图的路由"{route_path}"应该以/开头'
        # 获取endpoint
        endpoint = kwargs.pop('endpoint', None) or view_func.__name__
        # 判断本实例是否为子应用,那么就应该加上子应用名字
        if isinstance(self, SubApplication):
            endpoint = f'{self.name}.{endpoint}'
        # 实例化一个路由
        route = Route(route_path, methods, endpoint, **kwargs)
        # 添加到路由映射表中
        self.router.add_route(route_path, route, view_func)

    def adapt_view_func(self, func_or_class, methods):
        # 利用inspect 检查是否为类或者函数
        if inspect.isclass(func_or_class) and issubclass(func_or_class, View):
            # 当视图为 View 视图的子类时 methods 应该为None
            if methods is not None:
                raise ValueError(f'使用类视图 {func_or_class} 不应传入methods 参数')
            view = func_or_class()
            methods_list = view.get_view_methods
        elif inspect.isfunction(func_or_class):
            view = func_or_class
            methods_list = methods
        else:
            raise ValueError(f'视图应该为一个函数或View的子类 而不是{func_or_class}')

        return view, methods_list

    def new_route(self, route_path: str, methods=None, **kwargs):
        def decorator(func_or_class):
            view, methods_list = self.adapt_view_func(func_or_class, methods)
            # 调用set_route方法
            self.__set_route(route_path, view, methods_list, **kwargs)
            return func_or_class

        return decorator

    def get(self, route_path, **kwargs):
        """
        注册视图允许的请求方法仅为get的快捷方式
        """
        return self.new_route(route_path, ['GET'], **kwargs)

    def post(self, route_path, **kwargs):
        """
        注册视图允许的请求方法仅为post的快捷方式
        """
        return self.new_route(route_path, ['POST'], **kwargs)

    def put(self, route_path, **kwargs):
        """
        注册视图允许的请求方法仅为put的快捷方式
        """
        return self.new_route(route_path, ['PUT'], **kwargs)

    def patch(self, route_path, **kwargs):
        """
        注册视图允许的请求方法仅为patch的快捷方式
        """
        return self.new_route(route_path, ['PATCH'], **kwargs)

    def delete(self, route_path, **kwargs):
        """
        注册视图允许的请求方法仅为delete的快捷方式
        """
        return self.new_route(route_path, ['DELETE'], **kwargs)

    def add_route(self, route_path: str, func_or_class, methods=None, **kwargs):
        self.new_route(route_path, methods, **kwargs)(func_or_class)
        return self


class Application(Base):
    def __init__(self):
        super(Application, self).__init__()
        # 获取error_handler 捕获全局错误
        self.error_handler = import_from_str(conf.ERROR_HANDLER)
        # 初始化视图中间件列表
        self.__handle_view_middlewares = []
        # 请求的入口函数
        self.entry_func = None
        # 子应用列表
        self.sub_applications = {}
        self._setup()

    def _setup(self):
        # 配置logging
        setup_log_config(conf.LOGGING)
        if conf.USE_STATIC:
            self.new_route(conf.STATIC_URL + '{fp:path}', endpoint=conf.STATIC_ENDPOINT)(StaticView)
        # 初始化中间件
        self.initialize_middleware_stack()
        # 将on_stop_down信号的emit方法注册到atexit中
        atexit.register(on_stop_down.emit, sender=self)

    def initialize_middleware_stack(self):
        """
        初始化中间件 接收到的请求将会传入self.middlewares进行处理
        当配置文件中没有中间件时 self.middlewares 为 conv_exc_to_response
        :return:
        """
        # 将实例方法 get_response 包裹在全局异常处理转换器中
        get_response_func = conv_exc_to_response(self.__get_response, self.error_handler)

        for m_str in reversed(conf.MIDDLEWARES):
            # 导入中间件类
            middleware_class = import_from_str(m_str)
            if not issubclass(middleware_class, BaseMiddleware):
                raise ValueError(f"{m_str}应该为BaseMiddleware的子类而不是{type(middleware_class).__name__}")
            # 实例化中间件 将handler传入其中
            middleware_instance = middleware_class(get_response_func)
            # 如果该中间件有handle_view方法 那么就添加到__handle_view_middlewares中
            if hasattr(middleware_instance, 'handle_view'):
                self.__handle_view_middlewares.insert(0, middleware_instance.handle_view)
            # 将中间件包裹在全局异常处理转换器中
            get_response_func = conv_exc_to_response(middleware_instance, self.error_handler)
        self.entry_func = get_response_func

    def __get_response(self, *args, **kwargs):
        """
        获取response  如果任意一个中间件定义了 handle_view方法  该方法会调用调用handle_view
        如果 所有的handle_view返回值都是None 那么 请求会进入 对应的视图函数

        当 视图函数返回一个非 BaseResponse 或 BaseResponse子类实例时
        raise NonResponseException

        :param request:
        :return:
        """
        response = None
        # 获取到对应的处理视图和该视图所需的参数
        view_func, view_kwargs = self.__dispatch_route()
        view_kwargs.update(kwargs)
        handle_view_middlewares = self.__handle_view_middlewares.copy()
        # 执行handle_view
        for view_handler in handle_view_middlewares:
            response = view_handler(view_func, **view_kwargs)
            if response:
                break

        # 如果handle_view 没有返回response 那么就交给视图函数处理
        if not response:
            response = view_func(**view_kwargs)
        # 当视图返回的不是BaseResponse 或 BaseResponse子类的实例 raise NonResponseException
        if not isinstance(response, BaseResponse):
            raise NonResponseException(f"{view_func}视图没有返回正确的响应")

        return response

    def start(self):
        """
        开启服务
        :return: None
        """
        assert self.router.endpoint_func_dic, '未能找到至少一个以上的视图处理请求'
        # 判断是否使用热重载
        if conf.AUTO_RELOAD:
            run_in_reloader(
                self._inner_run,
                conf.AUTO_RELOAD_INTERVAL,
            )
        else:
            self._inner_run()

    def _inner_run(self):
        logger.warning(
            f"你的服务运行于:http://{conf.DEFAULT_HOST}:{conf.DEFAULT_PORT}/\n"
            f"- 请勿用于生产环境\n"
            f"- 版本号:{__version__}\n"
        )
        httpd = make_server(conf.DEFAULT_HOST, conf.DEFAULT_PORT, self)
        httpd.serve_forever()

    def include(self, sub: t.Union[str, "SubApplication"]):
        """
        将子应用挂载到Application中,类似Flask的register_blueprint
        """
        if isinstance(sub, str):
            sub = import_from_str(sub)
        if not isinstance(sub, SubApplication):
            raise TypeError(f"参数 sub类型不应该为{type(sub)}")
        if sub.name in self.sub_applications.keys():
            raise ValueError(f'挂载子应用失败 {sub}:不能出现重复的子应用名称 <{sub.name}>')

        self.router.include_router(sub.router)  # 将子应用的路由包含到主路由中
        sub.root = self  # 对子应用进行绑定
        self.sub_applications[sub.name] = sub

    def __dispatch_route(self):
        """
        根据请求信息找到处理该url的视图函数
        :return:一个视图函数
        """
        view_function, view_kwargs = self.router.match()
        return view_function, view_kwargs

    def _finish_response(self, response: BaseResponse, start_response: callable) -> t.Iterable[bytes]:
        """
        处理response 调用start_response设置响应状态码和响应头
        :param response: 封装的response 详情请看Vank/core/http/response
        :param start_response: WSGI规范的start_response
        :return: Iterable[bytes] 返回的body数据
        """
        # 默认的output的header参数是”Set-Cookie:“
        # 我们只需要后面的值而不需要”Set-Cookie:“
        # 所以应该将header行参设置为空
        headers = response.headers.items()
        # 设置cookie
        headers.extend([("Set-Cookie", cookie.output(header="")) for cookie in response.cookies.values()])
        start_response(response.status, headers)
        return response

    def __call__(self, environ: dict, start_response: callable) -> t.Iterable[bytes]:
        """
        被WSGI Server调用
        :param environ:环境变量以及请求参数等
        :param start_response:WSGI规范的一个function 我们需要给他设置响应码以及响应头等信息
        :return:作为响应数据
        """
        app_token = application._wrapped.set(self)  # noqa
        request_token = request._wrapped.set(Request(environ))  # noqa
        # 请求开始信号
        on_request_start.emit(self)
        response = self.entry_func()
        # 请求结束信号
        on_request_end.emit(self, response=response)
        # 关闭request的资源
        request.close()
        request._wrapped.reset(request_token)  # noqa
        application._wrapped.reset(app_token)  # noqa
        return self._finish_response(response, start_response)

    def url_reflect(self, endpoint: str, **kwargs):
        return self.router.url_reflect(endpoint, **kwargs)


class SubApplication(Base):
    def __init__(self, name, prefix: t.Optional[str] = None):
        super(SubApplication, self).__init__()
        self.name = name
        self.prefix = prefix
        if self.prefix:
            assert not self.prefix.endswith('/'), "url前缀不应以 '/'结尾"
            assert self.prefix.startswith('/'), "url前缀应以 '/'开头"
        self.root: "Application" = None

    def new_route(self, route_path: str, methods=None, **kwargs):
        if self.prefix:
            assert route_path.startswith("/"), f'子应用{self.name}视图的路由"{route_path}"应该以/开头'
            route_path = self.prefix + route_path
        return super(SubApplication, self).new_route(route_path, methods, **kwargs)

    def url_reflect(self, endpoint: str, **kwargs):
        if not isinstance(self.root, Application):
            raise TypeError(f'url_reflect失败,root的类型应为{type(Application).__name__} 你忘记挂载了吗?')
        return self.root.url_reflect(endpoint, **kwargs)