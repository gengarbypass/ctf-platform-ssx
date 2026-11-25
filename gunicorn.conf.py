import os


bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = int(os.environ.get("CTF_WEB_WORKERS", "2"))
threads = int(os.environ.get("CTF_WEB_THREADS", "4"))
preload_app = True
timeout = 60
graceful_timeout = 20
accesslog = "-"
errorlog = "-"


def post_worker_init(worker):
    from ctf_platform.views import start_awdp_scheduler

    start_awdp_scheduler(worker.wsgi)
