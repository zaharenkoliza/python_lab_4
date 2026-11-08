"""Замкнутая модель: N пользователей без think time. Сценарий задаётся WORKLOAD (rows|slow)."""
import os
import random

from locust import HttpUser, task, constant

WORKLOAD = os.environ.get("WORKLOAD", "rows")
SEED = int(os.environ.get("SEED", "0"))


class ApiUser(HttpUser):
    wait_time = constant(0)

    _n = 0

    def on_start(self):
        # воспроизводимый поток параметров: seed + порядковый номер пользователя в процессе
        ApiUser._n += 1
        self.rng = random.Random(SEED * 100003 + ApiUser._n)

    @task
    def call_endpoint(self):
        if WORKLOAD == "rows":
            self.client.get(f"/rows?start={self.rng.randint(1, 9900)}", name="/rows")
        else:
            self.client.get("/slow", name="/slow")
