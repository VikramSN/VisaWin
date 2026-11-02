import asyncio
import math
import random
from patchright.async_api import Page


async def human_delay(min_ms: int = 60, max_ms: int = 180) -> None:
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


async def human_type(page: Page, selector: str, text: str) -> None:
    loc = page.locator(selector).first
    await loc.click()
    await loc.fill("")  # Ensure the field is cleared to avoid double typing
    await human_delay(100, 300)
    for char in text:
        await loc.type(char, delay=random.uniform(50, 150))
    await human_delay(80, 200)


async def human_click(page: Page, selector: str) -> None:
    element = page.locator(selector).first
    box = await element.bounding_box()
    if box:
        x = box["x"] + box["width"]  * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await human_mouse_move(page, x, y)
        await human_delay(50, 150)
        await page.mouse.click(x, y)
    else:
        await element.click()
    await human_delay(100, 250)


# ─── Bezier-curve mouse movement utilities ──────────────────────────────────
# Cloudflare Turnstile's "Precursor" engine (Sept 2026) tracks mouse trajectories
# and rejects straight-line or instant-teleport movements. These helpers generate
# realistic curved paths with speed variation to pass behavioral analysis.


def _bezier_point(t: float, p0: tuple, p1: tuple, p2: tuple, p3: tuple) -> tuple:
    """Evaluate a cubic Bezier curve at parameter t ∈ [0, 1]."""
    u = 1 - t
    return (
        u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
        u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],
    )


def bezier_path(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int = 0,
) -> list[tuple[float, float]]:
    """Generate a smooth Bezier-curved path from *start* to *end*.

    The two control points are placed at random offsets so the curve looks
    organic — never a straight line.  ``steps`` defaults to a distance-based
    value (≈ 18–45 points) so short and long moves both look natural.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dist = math.hypot(dx, dy)
    if steps <= 0:
        steps = max(18, min(int(dist / 8), 45))

    # Control points offset perpendicular to the straight line for curvature
    spread_x = max(40, abs(dx) * 0.4)
    spread_y = max(40, abs(dy) * 0.4)
    cp1 = (
        start[0] + dx * random.uniform(0.15, 0.4) + random.uniform(-spread_x, spread_x) * 0.5,
        start[1] + dy * random.uniform(0.15, 0.4) + random.uniform(-spread_y, spread_y) * 0.5,
    )
    cp2 = (
        start[0] + dx * random.uniform(0.6, 0.85) + random.uniform(-spread_x, spread_x) * 0.35,
        start[1] + dy * random.uniform(0.6, 0.85) + random.uniform(-spread_y, spread_y) * 0.35,
    )

    points: list[tuple[float, float]] = []
    for i in range(steps + 1):
        # Ease-in-out timing to mimic natural hand acceleration / deceleration
        t_linear = i / steps
        t = 3 * t_linear**2 - 2 * t_linear**3  # smoothstep
        px, py = _bezier_point(t, start, cp1, cp2, end)
        # Tiny per-point jitter (sub-pixel noise like a real hand)
        px += random.gauss(0, 0.6)
        py += random.gauss(0, 0.6)
        points.append((px, py))
    return points


async def human_mouse_move(
    page: Page,
    target_x: float,
    target_y: float,
    *,
    start_x: float | None = None,
    start_y: float | None = None,
) -> None:
    """Move the mouse from *start* (or a random viewport position) to *(target_x, target_y)*
    along a Bezier curve with randomised speed that mimics a real hand movement.
    """
    if start_x is None or start_y is None:
        # Pick a plausible starting position if we don't know the current one
        vp = page.viewport_size or {"width": 1280, "height": 800}
        start_x = random.uniform(vp["width"] * 0.1, vp["width"] * 0.7)
        start_y = random.uniform(vp["height"] * 0.1, vp["height"] * 0.5)
        # Move to starting position first (simulates idle cursor position)
        await page.mouse.move(start_x, start_y)
        await human_delay(100, 400)

    path = bezier_path((start_x, start_y), (target_x, target_y))
    for px, py in path:
        await page.mouse.move(px, py)
        # Variable inter-step delay (faster in the middle, slower at edges)
        await asyncio.sleep(random.uniform(0.004, 0.018))

    # Final sub-pixel settle (hand micro-tremor on arrival)
    await page.mouse.move(
        target_x + random.uniform(-1.2, 1.2),
        target_y + random.uniform(-1.2, 1.2),
    )
    await asyncio.sleep(random.uniform(0.02, 0.06))
    await page.mouse.move(target_x, target_y)


async def human_scroll(page: Page, amount: int = 0) -> None:
    """Perform a tiny random scroll to simulate a human glancing at the page.

    This generates scroll events that Cloudflare's behavioral engine expects
    from a real user before they interact with the Turnstile checkbox.
    """
    if amount == 0:
        amount = random.choice([-80, -60, -40, 40, 60, 80, 100, 120])
    vp = page.viewport_size or {"width": 1280, "height": 800}
    x = random.uniform(vp["width"] * 0.3, vp["width"] * 0.7)
    y = random.uniform(vp["height"] * 0.3, vp["height"] * 0.7)
    await page.mouse.move(x, y)
    await human_delay(200, 500)
    await page.mouse.wheel(0, amount)
    await human_delay(300, 700)
