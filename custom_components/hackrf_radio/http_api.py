"""Authenticated admin-only capture downloads through Home Assistant."""

from aiohttp import web
from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView

from .radio import RadioError
from .websocket_api import coordinator_for


class CaptureView(HomeAssistantView):
    url = "/api/hackrf_radio/{entry_id}/captures/{capture_id}"
    name = "api:hackrf_radio:capture"
    requires_auth = True

    async def get(self, request, entry_id: str, capture_id: str):
        if not request[KEY_HASS_USER].is_admin:
            raise web.HTTPForbidden
        try:
            coordinator = coordinator_for(request.app[KEY_HASS], entry_id)
            metadata, raw = await coordinator.client.capture_data(coordinator.serial, capture_id)
        except RadioError as err:
            raise web.HTTPNotFound(text=str(err)) from err
        return web.Response(body=raw, content_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="hackrf-{metadata["capture_id"]}.cs8"',
            "X-HackRF-Format": "ci8", "X-HackRF-SHA256": metadata["sha256"],
        })
