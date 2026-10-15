from __future__ import annotations

import unittest

from fastapi.routing import APIRoute

from app.api.deps import current_user
from app.api import routes_assignments
from app.main import app


def _api_routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _api_routes(included.routes)


def _depends_on(route: APIRoute, dependency) -> bool:
    def visit(dependant) -> bool:
        return dependant.call is dependency or any(visit(child) for child in dependant.dependencies)

    return visit(route.dependant)


class TaskRouteAuthenticationContractTests(unittest.TestCase):
    def test_every_task_route_requires_an_active_authenticated_user(self) -> None:
        task_routes = [
            route
            for route in _api_routes(app.routes)
            if "tasks" in (route.tags or [])
        ]

        self.assertTrue(task_routes, "the task router must be registered")
        unprotected = [
            f"{','.join(sorted(route.methods or []))} {route.path}"
            for route in task_routes
            if not _depends_on(route, current_user)
        ]
        self.assertEqual(unprotected, [])

    def test_every_internal_card_and_assignment_route_requires_authentication(self) -> None:
        routes = [route for route in routes_assignments.router.routes if isinstance(route, APIRoute)]
        self.assertTrue(routes, "the internal workflow router must expose routes")
        unprotected = [
            f"{','.join(sorted(route.methods or []))} {route.path}"
            for route in routes
            if not _depends_on(route, current_user)
        ]
        self.assertEqual(unprotected, [])


if __name__ == "__main__":
    unittest.main()
