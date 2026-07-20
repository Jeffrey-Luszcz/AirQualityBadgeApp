import sys
import os

# Air Quality Badge App
# https://github.com/Jeffrey-Luszcz/AirQualityBadgeApp/
# SPDX-License-Identifier: MIT

sys.path.insert(0, "/system/apps/air_quality")
os.chdir("/system/apps/air_quality")

from badgeware import io, brushes, shapes, screen, PixelFont, Image, run
import network
from urllib.urequest import urlopen
import json
import gc

# ---------------------------------------------------------------------------
# USER CONFIGURATION
# ---------------------------------------------------------------------------
# #1 Your PurpleAir sensor / station ID.
#    Find it on the PurpleAir map: click a sensor -> "Get This Widget" and read
#    the numeric fragment from "PurpleAirWidget_12345" (here you'd enter 12345).
PURPLE_AIR_SENSOR = "12345"

# #1b Optional SECOND sensor. Leave as "" to show only the first sensor. When
#     set, use the UP/DOWN buttons to cycle between the two sensors.
PURPLE_AIR_SENSOR_2 = ""

# #2 Your PurpleAir API READ key. Keep this secret! Do not publish or email it.
#    Get one at https://develop.purpleair.com/keys
PURPLE_AIR_API_KEY = "00000000-0000-0000-0000-000000000000"

# #3 How often to refresh the reading, in SECONDS (default: 600 = 10 minutes).
REFRESH_SECONDS = 600

# #4 Which PurpleAir PM2.5 field/averaging window to read. This controls how the
#    reading is smoothed and lets you match the PurpleAir map's averaging.
#    Common options (raw ATM values the EPA correction is applied to):
#      "pm2.5_a"        live channel A (least smoothed)
#      "pm2.5"          live A+B average
#      "pm2.5_10minute" 10-minute average
#      "pm2.5_60minute" 60-minute average
#      "pm2.5_24hour"   24-hour average
#      "pm2.5_1week"    7-day average  (matches map links with p604800)
PM_FIELD = "pm2.5"

# #5 Apply the US EPA (Barkjohn) correction so the AQI matches PurpleAir's map
#    "US EPA AQI" layer. Requires the sensor's humidity reading.
#      PM_corrected = 0.524 * pm_cf1 - 0.0862 * humidity + 5.75
#    Set to False to match the website's raw/live AQI (conversion "None").
APPLY_EPA_CORRECTION = False

# WiFi credentials are NOT stored here. They are loaded from /secrets.py
# (see secrets.py.example). Only the PurpleAir key lives in this file.
# ---------------------------------------------------------------------------

# Fonts
small_font = PixelFont.load("/system/assets/fonts/ark.ppf")
large_font = PixelFont.load("/system/assets/fonts/absolute.ppf")

# Fixed UI colors
white = brushes.color(255, 255, 255)
background = brushes.color(13, 17, 23)
gray = brushes.color(120, 130, 140)
error_bg = brushes.color(60, 0, 0)

# US EPA AQI category colors (index matches category returned by aqi_category)
AQI_COLORS = (
    (0, 228, 0),      # Good                          0-50
    (255, 255, 0),    # Moderate                      51-100
    (255, 126, 0),    # Unhealthy for Sensitive Grps  101-150
    (255, 0, 0),      # Unhealthy                     151-200
    (143, 63, 151),   # Very Unhealthy                201-300
    (126, 0, 35),     # Hazardous                     301+
)

# WiFi state
WIFI_TIMEOUT = 60  # seconds
WIFI_SSID = None
WIFI_PASSWORD = None
wlan = None
wifi_ticks_start = None
connected = False
wifi_failed = False

# App state
# List of sensor IDs to display (second one only if it's non-empty).
SENSORS = [PURPLE_AIR_SENSOR]
if PURPLE_AIR_SENSOR_2 and str(PURPLE_AIR_SENSOR_2).strip():
    SENSORS.append(str(PURPLE_AIR_SENSOR_2).strip())

current_idx = 0          # index into SENSORS of the sensor on screen
last_fetch = None        # io.ticks (ms) of last fetch attempt (single timer)
loading = False
# Per-sensor cache: sensor id -> dict(aqi, pm_raw, pm_corrected, humidity,
# name, error). A sensor is "seen" once it has an entry here.
readings = {}

split_view = False       # False = single sensor; True = both side-by-side


# ---------------------------------------------------------------------------
# WiFi
# ---------------------------------------------------------------------------
def get_wifi_credentials():
    """Load WiFi credentials from /secrets.py. Returns True if present."""
    global WIFI_SSID, WIFI_PASSWORD

    if WIFI_SSID is not None:
        return True

    try:
        sys.path.insert(0, "/")
        from secrets import WIFI_SSID as S, WIFI_PASSWORD as P
        WIFI_SSID = S
        WIFI_PASSWORD = P
        sys.path.pop(0)
    except ImportError:
        WIFI_SSID = None
        WIFI_PASSWORD = None

    return WIFI_SSID is not None


def wlan_start():
    """Non-blocking WiFi connect. Returns True when connected, False on timeout."""
    global wlan, wifi_ticks_start, connected, wifi_failed

    if connected:
        return True

    if wifi_ticks_start is None:
        wifi_ticks_start = io.ticks

    if wlan is None:
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        if not wlan.isconnected():
            wlan.connect(WIFI_SSID, WIFI_PASSWORD)
            print("Connecting to WiFi...")

    if wlan.isconnected():
        connected = True
        print("WiFi connected:", wlan.ifconfig()[0])
        return True

    if io.ticks - wifi_ticks_start > WIFI_TIMEOUT * 1000:
        wifi_failed = True
        return False

    return False


# ---------------------------------------------------------------------------
# PM2.5 -> US EPA AQI
# ---------------------------------------------------------------------------
# (C_low, C_high, I_low, I_high) piecewise-linear breakpoints for PM2.5.
_AQI_BREAKPOINTS = (
    (0.0, 12.0, 0, 50),
    (12.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
)


def pm25_to_aqi(pm):
    """Convert a raw PM2.5 concentration (ug/m3) to a US EPA AQI integer."""
    if pm is None:
        return None
    if pm < 0:
        pm = 0.0
    # EPA rounds the concentration to one decimal place before conversion.
    c = int(pm * 10 + 0.5) / 10.0
    for c_low, c_high, i_low, i_high in _AQI_BREAKPOINTS:
        if c <= c_high:
            aqi = (i_high - i_low) / (c_high - c_low) * (c - c_low) + i_low
            return int(aqi + 0.5)
    # Above the top breakpoint: cap at 500 (beyond the AQI scale).
    return 500


def epa_correct(pm_cf1, rh):
    """Apply the US EPA (Barkjohn 2021) correction used by PurpleAir's map.

    PM_corrected = 0.524 * pm_cf1 - 0.0862 * humidity + 5.75
    Falls back to the uncorrected value if humidity is unavailable.
    """
    if rh is None:
        return pm_cf1
    corrected = 0.524 * pm_cf1 - 0.0862 * rh + 5.75
    return corrected if corrected > 0 else 0.0


def aqi_category(aqi):
    """Map an AQI value to a category index (0-5) for color selection."""
    if aqi <= 50:
        return 0
    if aqi <= 100:
        return 1
    if aqi <= 150:
        return 2
    if aqi <= 200:
        return 3
    if aqi <= 300:
        return 4
    return 5


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------
def fetch_air_quality(sensor_id):
    """Fetch PM2.5 (and humidity) for one sensor and cache it in `readings`."""
    global loading, last_fetch

    loading = True
    last_fetch = io.ticks

    prev = readings.get(sensor_id)
    prev_aqi = prev["aqi"] if prev else None

    entry = {
        "aqi": None, "pm_raw": None, "pm_corrected": None,
        "humidity": None, "name": None, "error": None, "trend": None,
    }

    response = None
    try:
        # p25aqic (the RGB color string) is only in the legacy widget JSON, not
        # in the v1 API, so we read a PM2.5 field (PM_FIELD), optionally apply
        # the US EPA correction, and derive the color from the AQI category.
        fields = "name," + PM_FIELD
        if APPLY_EPA_CORRECTION:
            fields += ",humidity"
        url = "https://api.purpleair.com/v1/sensors/%s?fields=%s" % (
            sensor_id,
            fields,
        )
        response = urlopen(
            url,
            headers={
                "X-API-Key": PURPLE_AIR_API_KEY,
                "User-Agent": "GitHubBadge",
            },
        )

        data = b""
        chunk = bytearray(512)
        while True:
            length = response.readinto(chunk)
            if length == 0:
                break
            data += chunk[:length]

        result = json.loads(data.decode("utf-8"))
        sensor = result["sensor"]

        entry["name"] = sensor.get("name")

        # Real-time fields (pm2.5, pm2.5_a) sit directly on "sensor"; averaged
        # windows (pm2.5_10minute ... pm2.5_1week) are nested under "stats".
        if PM_FIELD in sensor:
            pm_raw = float(sensor[PM_FIELD])
        else:
            pm_raw = float(sensor["stats"][PM_FIELD])
        entry["pm_raw"] = pm_raw

        if APPLY_EPA_CORRECTION:
            rh = sensor.get("humidity")
            if rh is not None:
                rh = float(rh)
            entry["humidity"] = rh
            entry["pm_corrected"] = epa_correct(pm_raw, rh)
        else:
            entry["pm_corrected"] = pm_raw

        entry["aqi"] = pm25_to_aqi(entry["pm_corrected"])
        # Trend vs the previous reading for this sensor (None on first read).
        if prev_aqi is not None:
            if entry["aqi"] > prev_aqi:
                entry["trend"] = "up"
            elif entry["aqi"] < prev_aqi:
                entry["trend"] = "down"
            else:
                entry["trend"] = "same"
        print("[%s] %s=%s corrected=%s (RH=%s) -> AQI=%s" % (
            sensor_id, PM_FIELD, pm_raw, entry["pm_corrected"],
            entry["humidity"], entry["aqi"]))

        del data, chunk, result, sensor
    except Exception as e:
        print("Error fetching air quality for %s:" % sensor_id, e)
        entry["error"] = str(e)
    finally:
        readings[sensor_id] = entry
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        loading = False
        gc.collect()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def center_text(text, y):
    w, _ = screen.measure_text(text)
    screen.text(text, int(80 - w / 2), y)


def center_text_in(text, cx, y):
    w, _ = screen.measure_text(text)
    screen.text(text, int(cx - w / 2), y)


def fill_screen(brush):
    screen.brush = brush
    screen.draw(shapes.rectangle(0, 0, 160, 120))


# Cache for the enlarged AQI number image so we don't re-render every frame.
_big_num_cache = {"key": None, "img": None, "w": 0, "h": 0}
# Disabled automatically if off-screen image rendering fails on the device,
# so we fall back to plain (unscaled) text instead of erroring every frame.
_big_render_ok = True


def _big_number_image(text):
    """Render `text` in large_font to an off-screen image (cached by text)."""
    if _big_num_cache["key"] == text and _big_num_cache["img"] is not None:
        return _big_num_cache["img"], _big_num_cache["w"], _big_num_cache["h"]

    screen.font = large_font
    w, h = screen.measure_text(text)
    w = max(1, int(w))
    h = max(1, int(h)) + 2  # small vertical pad so tall glyphs don't clip
    img = Image(0, 0, w, h)  # 4-arg form matches shipped badge apps
    img.font = large_font
    img.brush = white
    img.text(text, 0, 0)

    _big_num_cache.update({"key": text, "img": img, "w": w, "h": h})
    return img, w, h


def _draw_big_aqi(number):
    """Draw the AQI number at 2x, falling back to plain text if unsupported."""
    global _big_render_ok
    if _big_render_ok:
        try:
            img, w, h = _big_number_image(number)
            bw, bh = w * 2, h * 2
            screen.scale_blit(img, int(80 - bw / 2), int(52 - bh / 2), bw, bh)
            return
        except Exception as e:
            print("Big-number render failed, using plain text:", e)
            _big_render_ok = False

    # Fallback: plain large_font number, centered.
    screen.font = large_font
    screen.brush = white
    w, h = screen.measure_text(number)
    screen.text(number, int(80 - w / 2), int(52 - h / 2))


def _draw_trend_arrow(direction, cx, cy, s=1.0):
    """Draw a white trend arrow (up/down/right) built from thick lines."""
    if not direction:
        return
    screen.brush = white
    t = max(1, int(round(3 * s)))
    a = 7 * s   # half-length along the arrow
    b = 5 * s   # head spread
    c = 2 * s   # head inset
    try:
        if direction == "up":
            screen.draw(shapes.line(cx, cy + a, cx, cy - a, t))
            screen.draw(shapes.line(cx, cy - a, cx - b, cy - c, t))
            screen.draw(shapes.line(cx, cy - a, cx + b, cy - c, t))
        elif direction == "down":
            screen.draw(shapes.line(cx, cy - a, cx, cy + a, t))
            screen.draw(shapes.line(cx, cy + a, cx - b, cy + c, t))
            screen.draw(shapes.line(cx, cy + a, cx + b, cy + c, t))
        else:  # "same" -> right-pointing arrow
            screen.draw(shapes.line(cx - a, cy, cx + a, cy, t))
            screen.draw(shapes.line(cx + a, cy, cx + c, cy - b, t))
            screen.draw(shapes.line(cx + a, cy, cx + c, cy + b, t))
    except Exception as e:
        print("Trend arrow render failed:", e)


def draw_connecting():
    fill_screen(background)
    dots = "." * (int(io.ticks / 500) % 4)
    screen.font = large_font
    screen.brush = white
    center_text("Air Quality", 10)
    screen.font = small_font
    center_text("Jeff Luszcz", 30)
    screen.font = large_font
    center_text("Connecting", 60)
    screen.font = small_font
    center_text("to WIFI" + dots, 82)


def draw_message(title, subtitle, brush):
    fill_screen(brush)
    screen.font = large_font
    screen.brush = white
    center_text(title, 40)
    if subtitle:
        screen.font = small_font
        screen.brush = white
        center_text(subtitle, 62)


def draw_aqi(entry):
    aqi_value = entry["aqi"]
    pm25_corrected = entry["pm_corrected"]
    sensor_name = entry["name"]

    color = AQI_COLORS[aqi_category(aqi_value)]
    fill_screen(brushes.color(color[0], color[1], color[2]))

    # Small "AQI" label near the top, plus a page indicator when cycling.
    screen.font = small_font
    screen.brush = white
    center_text("AQI", 12)
    if len(SENSORS) > 1:
        page = "%d/%d" % (current_idx + 1, len(SENSORS))
        w, _ = screen.measure_text(page)
        screen.text(page, int(156 - w), 4)

    # Large AQI number, rendered at 2x (with a plain-text fallback), centered.
    _draw_big_aqi(str(aqi_value))

    # Trend arrow vs the previous reading, on the right of the AQI row.
    _draw_trend_arrow(entry.get("trend"), 146, 52)

    # Sensor name and PM2.5 value in white near the bottom (same font).
    screen.font = large_font
    screen.brush = white
    if sensor_name:
        center_text(sensor_name[:20], 80)
    if pm25_corrected is not None:
        label = "PM2.5c" if APPLY_EPA_CORRECTION else "PM2.5"
        center_text("%s: %.1f" % (label, pm25_corrected), 96)


def _draw_split_panel(entry, x0, idx):
    """Draw one sensor into a half-width column (width 80) starting at x0."""
    cx = x0 + 40
    if entry and entry["aqi"] is not None:
        color = AQI_COLORS[aqi_category(entry["aqi"])]
    else:
        color = background if not (entry and entry["error"]) else error_bg
    screen.brush = brushes.color(color[0], color[1], color[2])
    screen.draw(shapes.rectangle(x0, 0, 80, 120))

    screen.brush = white
    screen.font = small_font

    # Sensor label (name if known, else the station number), truncated to fit.
    name = (entry["name"] if entry and entry["name"] else SENSORS[idx])
    center_text_in(name[:12], cx, 8)

    if entry and entry["aqi"] is not None:
        center_text_in("AQI", cx, 30)
        # AQI number in the large font (no 2x, to fit the narrow column).
        screen.font = large_font
        center_text_in(str(entry["aqi"]), cx, 46)
        _draw_trend_arrow(entry.get("trend"), cx, 88, 0.7)
        screen.font = small_font
        if entry["pm_corrected"] is not None:
            lbl = "PM2.5c" if APPLY_EPA_CORRECTION else "PM2.5"
            center_text_in("%s %.1f" % (lbl, entry["pm_corrected"]), cx, 104)
    elif entry and entry["error"]:
        center_text_in("Error", cx, 56)
    else:
        center_text_in("...", cx, 56)


def draw_split(e1, e2):
    """Alternative view: sensor 1 on the left, sensor 2 on the right."""
    _draw_split_panel(e1, 0, 0)
    _draw_split_panel(e2, 80, 1)
    # White divider down the middle.
    screen.brush = white
    screen.draw(shapes.rectangle(79, 0, 2, 120))


# ---------------------------------------------------------------------------
# MonaOS lifecycle
# ---------------------------------------------------------------------------
def init():
    print("Air Quality Monitor starting...")


def update():
    try:
        _update()
    except Exception as e:
        import sys
        sys.print_exception(e)
        try:
            draw_message("App Error", str(e)[:22], error_bg)
        except Exception:
            pass


def _update():
    global current_idx, split_view

    # 1. Need WiFi credentials.
    if not get_wifi_credentials():
        draw_message("No WiFi", "Edit secrets.py", error_bg)
        return

    # 2. Connect to WiFi (non-blocking), showing the connecting screen.
    if not connected:
        if wlan_start():
            # Just connected: show a Loading screen this frame and defer the
            # (blocking) fetch to the next frame so the UI doesn't appear hung.
            draw_message("Loading", "Reading sensor...", background)
        else:
            if wifi_failed:
                draw_message("WiFi Failed", "Check secrets.py", error_bg)
            else:
                draw_connecting()
        return

    # 3. Button handling (only when a second sensor is set):
    #      B      -> toggle split view <-> single view
    #      UP/DOWN-> cycle sensors (single view only)
    if len(SENSORS) > 1 and not loading:
        if io.BUTTON_B in io.pressed:
            split_view = not split_view
        if not split_view:
            if io.BUTTON_UP in io.pressed:
                current_idx = (current_idx - 1) % len(SENSORS)
            elif io.BUTTON_DOWN in io.pressed:
                current_idx = (current_idx + 1) % len(SENSORS)

    # Which sensors are on screen right now (both in split view, else one).
    visible = list(SENSORS) if (split_view and len(SENSORS) > 1) \
        else [SENSORS[current_idx]]

    # 4. Fetch each visible sensor when never seen, on manual refresh (A), or
    #    when the shared timer is due. Cached values within the interval are
    #    reused; off-screen sensors are never fetched. `due` is captured before
    #    fetching so both panels refresh together (a fetch resets last_fetch).
    if not loading:
        any_error = any(
            readings.get(s) and readings[s]["error"] for s in visible)
        interval = 15 if any_error else REFRESH_SECONDS
        manual = io.BUTTON_A in io.pressed
        due = last_fetch is None or io.ticks - last_fetch > interval * 1000
        for s in visible:
            if readings.get(s) is None or manual or due:
                fetch_air_quality(s)

    # 5. Render.
    if split_view and len(SENSORS) > 1:
        draw_split(readings.get(SENSORS[0]), readings.get(SENSORS[1]))
        return

    cur_entry = readings.get(SENSORS[current_idx])
    if cur_entry and cur_entry["aqi"] is not None:
        draw_aqi(cur_entry)
    elif cur_entry and cur_entry["error"]:
        draw_message("Sensor Error", cur_entry["error"][:22], error_bg)
    else:
        draw_message("Loading", "Reading sensor...", background)


def on_exit():
    print("Air Quality Monitor exiting.")


if __name__ == "__main__":
    run(update)
