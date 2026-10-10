from __future__ import annotations

import unittest

from fastapi.routing import APIRoute

from app.api.deps import current_user
from app.api.routes_tasks import router as task_router


def _dependency_calls(dependant):
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _dependency_calls(dependency)


class TaskRouteAuthenticationContractTests(unittest.TestCase):
    def test_every_task_route_requires_an_authenticated_user(self) -> None:
        routes = [route for route in task_router.routes if isinstance(route, APIRoute)]

        self.assertTrue(routes, "task router must expose at least one route")
        unauthenticated_routes = [
            route.path
            for route in routes
            if current_user not in set(_dependency_calls(route.dependant))
        ]

        self.assertEqual([], unauthenticated_routes)


if __name__ == "__main__":
    unittest.main()
