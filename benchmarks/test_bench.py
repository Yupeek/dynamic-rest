from __future__ import absolute_import

import json
import importlib
import random
import statistics
import string
from collections import defaultdict
from datetime import datetime

from rest_framework.test import APITestCase

from benchmarks_app.models import Group, Permission, User


DREST_VERSION = importlib.import_module('dynamic_rest')
DREST_VERSION = DREST_VERSION.__version__
DRF_VERSION = importlib.import_module('rest_framework').__version__
AVERAGE_TYPE = 'median'

# BENCHMARKS: configuration for benchmarks
BENCHMARKS = [
    {
        # name: benchmark name
        'name': 'linear',
        # drest: DREST endpoint
        'drest': '/drest/users/',
        # drf: DRF endpoint
        'drf': '/drf/users/',
        # min_size: minimum sample size
        'min_size': 1,
        # max_size: maximum sample size
        'max_size': 16,
        # multiplier: number of records in one sample
        'multiplier': 256,
        # samples: number of samples to take
        'samples': 12
    },
    {
        'name': 'quadratic',
        'drest': '/drest/users?include[]=groups.',
        'drf': '/drf/users_with_groups/',
        'min_size': 1,
        'max_size': 16,
        'multiplier': 4,
        'samples': 12
    },
    {
        'name': 'cubic',
        'drest': (
            '/drest/users/'
            '?include[]=groups.permissions.'
        ),
        'drf': '/drf/users_with_all/',
        'min_size': 1,
        'max_size': 16,
        'multiplier': 1,
        'samples': 12
    }
]

CHART_HEAD = """
<head>
    <script src="https://code.jquery.com/jquery-1.12.0.min.js"></script>
    <script src="https://code.highcharts.com/highcharts.js"></script>
</head>
"""

CHART_TEMPLATE = """
<script>
    $(function () {{
        var {benchmark_name}_chart = new Highcharts.Chart({{
            chart: {{
                renderTo: '{benchmark_name}',
                type: 'line',
            }},
            title: {{
                text: '{benchmark_name}',
                x: -20 //center
            }},
            xAxis: {{
                title: {{
                    text: '# of records'
                }}
            }},
            yAxis: {{
                title: {{
                    text: 'Response time (seconds)'
                }},
                plotLines: [{{
                    value: 0,
                    width: 1,
                    color: '#808080'
                }}]
            }},
            legend: {{
                layout: 'vertical',
                align: 'right',
                verticalAlign: 'middle',
                borderWidth: 0
            }},
            series: {data}
        }});
    }});
</script>
<div id="{benchmark_name}" style="width:100%"></div>
<br/>
"""


def get_average(values):
    if len(values) == 0:
        return 0
    elif AVERAGE_TYPE == 'mean':
        return statistics.mean(values)
    elif AVERAGE_TYPE == 'median':
        return statistics.median(values)


class BenchmarkTest(APITestCase):

    @classmethod
    def setUpClass(cls):
        # initialize results: a 4x nested dictionary
        cls._results = defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(
                    dict
                )
            )
        )

    @classmethod
    def tearDownClass(cls):
        # save results to an HTML file
        with open('benchmarks.html', 'w') as file:
            file.write(CHART_HEAD)
            for benchmark_name, implementations in sorted(
                cls._results.items()
            ):
                data = []
                for implementation_name, implementation_data in sorted(
                    implementations.items()
                ):
                    for key in implementation_data.keys():
                        values = sorted(implementation_data[key].values())
                        implementation_data[key] = get_average(values)

                    implementation_data = sorted(implementation_data.items())

                    data.append({
                        'name': implementation_name,
                        'data': implementation_data
                    })

                file.write(
                    CHART_TEMPLATE.format(
                        benchmark_name=benchmark_name,
                        data=json.dumps(data)
                    )
                )

    def bench(
        self,
        implementation_name,
        benchmark_name,
        url,
        size,
        sample
    ):
        start = datetime.now()
        response = self.client.get(url)
        end = datetime.now()
        self.assertEqual(response.status_code, 200)
        diff = end - start
        d = diff.total_seconds()
        self._results[benchmark_name][implementation_name][size][sample] = d

    def generate_linear(self, amount):
        users = [
            User(name=str(i)) for i in range(amount)
        ]
        User.objects.bulk_create(users)
        return len(users)

    def generate_quadratic(self, amount):
        users = [
            User(name=f"{i}")
            for i in range(amount)
        ]
        total = len(users)
        User.objects.bulk_create(users)
        users = User.objects.all()
        for user in users:
            groups = [
                Group(
                    name=f'{user.name}-{j}',
                    max_size=amount
                ) for j in range(amount)
            ]
            Group.objects.bulk_create(groups)
            total += len(groups)
            user.groups.set(groups)
        return total

    def generate_cubic(self, amount):
            users = [
                User(name=f"{i}")
                for i in range(amount)
            ]
            total = len(users)
            User.objects.bulk_create(users)
            users = User.objects.all()
            for user in users:
                groups = [
                    Group(
                        name=f'{user.name}-{j}',
                        max_size=amount
                    ) for j in range(amount)
                ]
                Group.objects.bulk_create(groups)
                total += len(groups)
                user.groups.set(groups)
                for group in groups:
                    permissions = [
                        Permission(
                            name=f'{user.name}-{group.name}-{k}'
                        ) for k in range(amount)
                    ]
                    Permission.objects.bulk_create(permissions)
                    total += len(permissions)
                    group.permissions.set(permissions)
            return total


def generate_benchmark(name, title, drest, drf, size, sample):
    def bench(self):
        total_size = getattr(self, 'generate_%s' % name)(size)
        self.bench(
            'DREST %s' % DREST_VERSION,
            title,
            drest,
            total_size,
            sample
        )
        self.bench('DRF %s' % DRF_VERSION, title, drf, total_size, sample)
    return bench


def get_random_string(size):
    return ''.join(
        random.choice(string.ascii_uppercase)
        for _ in range(size)
    )


# generate test methods
for benchmark in BENCHMARKS:
    name = benchmark['name']
    title = name.title()
    min_size = benchmark['min_size']
    max_size = benchmark['max_size']
    drf = benchmark['drf']
    drest = benchmark['drest']
    multiplier = benchmark['multiplier']
    samples = benchmark['samples']

    for size in range(min_size, max_size + 1):
        size *= multiplier
        for sample in range(samples):
            test_name = 'test_%s_%s_%d_%d' % (
                get_random_string(4), name, size, sample
            )
            test = generate_benchmark(name, title, drest, drf, size, sample)
            setattr(BenchmarkTest, test_name, test)
            del test_name, test
