---
tags: ["weather", "data-flow", "backend", "frontend", "api", "architecture"]
category: architecture
created: 2026-02-26T17:47:07.115425
---

# Weather Data Flow Complete Map - GenomeUI

# Weather Data Flow End-to-End Map

## Overview
The weather feature flows from user intent parsing through API calls to rendering. This document maps the exact line ranges and code for all critical decision points.

---

## BACKEND (main.py) - 30,309 lines total

### 1. Intent Parsing: `parse_semantic_command()` 
**File:** `c:\Users\steve\Documents\GenomeUI\backend\main.py`  
**Lines:** 16995-17026

**Purpose:** Detects weather intent from natural language and builds payload

```python
# Line 17010: Weather signal detection
weather_signal = any(word in lower for word in ("weather", "forecast", "temperature", "rain", "snow", "wind"))
if not weather_signal:
    return None

# Line 17014-17026: Location extraction and payload building
location = ""
match = re.search(r"\b(?:in|at|for)\s+(.+)$", text, flags=re.IGNORECASE)
if match:
    candidate = normalize_intent_text(match.group(1))
    if candidate.lower() not in {"today", "tomorrow", "now", "right now"}:
        location = candidate
if not location and any(phrase in lower for phrase in ("where i am", "my location", "where am i", "here")):
    location = "__current__"
if not location and re.match(r"^(what(?:'s| is)|whats|show|check|get|tell me|how(?:'s| is)|weather|forecast|temperature)\b", lower):
    location = "__current__"
if not location:
    return None
return {"type": "weather_forecast", "domain": "weather", "payload": {"location": location}}
```

**Key Decision Points:**
- Line 17010: Checks for weather keywords
- Line 17015-17019: Extracts location from "in/at/for" patterns
- Line 17020-17023: Detects relative location ("__current__" = auto-locate)
- **Payload Structure:** `{"type": "weather_forecast", "domain": "weather", "payload": {"location": str}}`

---

### 2. Location Normalization: `normalize_weather_location()`
**Lines:** 16985-16992

```python
def normalize_weather_location(raw: str) -> str:
    location = normalize_intent_text(raw)
    lower = location.lower()
    if not location or lower in {"current", "current location", "my location", "where i am", "here", "right now", "today"}:
        return resolve_user_location_hint()  # GPS fallback
    if location == "__current__":
        return resolve_user_location_hint()
    return location
```

**Logic:**
- If input is empty or matches relative phrases → calls `resolve_user_location_hint()` for GPS
- If input is "__current__" marker → calls `resolve_user_location_hint()`
- Otherwise returns normalized location as-is

---

### 3. Weather Data Fetch: `weather_read_snapshot()`
**Lines:** 2349-2508

**Purpose:** Fetches real weather data from open-meteo API

#### 3a. Input Processing (Lines 2349-2404)
```python
def weather_read_snapshot(location: str, provider_mode: str | None = None) -> dict[str, Any]:
    query = str(location or "").strip()
    if not query:
        query = resolve_user_location_hint()
    mode = normalize_connector_provider_mode(provider_mode or CONNECTOR_PROVIDER_MODE)
    if mode == "mock":
        return weather_mock_snapshot(query)  # Fallback to mock data
```

#### 3b. Geocoding Flow (Lines 2358-2403)
Two paths depending on input format:

**Path A: Raw Coordinates (lat,lon)**
```python
# Line 2359-2388: If query matches "lat,lon" pattern
coord_match = _COORD_RE.match(query)
if coord_match:
    lat = float(coord_match.group(1))
    lon = float(coord_match.group(2))
    # Reverse-geocode for display name (Nominatim OSM)
    rev = client.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={"lat": lat, "lon": lon, "format": "json", "zoom": 10}
    )
    # Extracts city, state, country
```

**Path B: City Name**
```python
# Line 2390-2403: Forward geocoding via open-meteo
geo = client.get(
    "https://geocoding-api.open-meteo.com/v1/search",
    params={"name": query, "count": 1, "language": "en", "format": "json"}
)
geo.raise_for_status()
geo_json = geo.json()
results = geo_json.get("results", [])
if not results:
    return weather_fallback_snapshot(query)  # Fallback if no match
hit = results[0]
lat = float(hit.get("latitude", 0.0) or 0.0)
lon = float(hit.get("longitude", 0.0) or 0.0)
name = str(hit.get("name", query) or query)
country = str(hit.get("country_code", "") or "")
```

#### 3c. Weather API Call (Lines 2404-2420)
```python
wx = client.get(
    "https://api.open-meteo.com/v1/forecast",
    params={
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,wind_speed_10m,weather_code",
        "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,weather_code",
        "daily": "sunrise,sunset",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "auto",
        "forecast_hours": 12,  # 12-hour forecast
    }
)
wx.raise_for_status()
wx_payload = wx.json()
```

**Open-Meteo Request Keys:**
- `latitude`, `longitude`: Geocoded coordinates
- `current`: Real-time temp, wind, weather code
- `hourly`: 12-hour forecast (temp, precip probability, wind, weather code)
- `daily`: Sunrise/sunset times
- `forecast_hours`: 12 hours
- Units: Fahrenheit, mph

#### 3d. Weather Code Mapping (Lines 2423-2454)
```python
_raw_code = current.get("weather_code")
code = int(_raw_code) if _raw_code is not None else -1
code_map = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    51: "light drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    95: "thunderstorm",
    # ... (26 total codes)
}
```

#### 3e. Hourly Data Processing (Lines 2455-2476)
```python
hourly_times = hourly.get("time", [])  # ISO format: "2026-02-25T15:00"
hourly_temp = hourly.get("temperature_2m", [])
hourly_wind = hourly.get("wind_speed_10m", [])
hourly_precip = hourly.get("precipitation_probability", [])
hourly_code = hourly.get("weather_code", [])

hourly_out: list[dict[str, Any]] = []
for idx in range(min(len(hourly_times), len(hourly_temp), len(hourly_wind), 12)):
    raw_time = str(hourly_times[idx] or "")
    hour_label = raw_time[-5:] if len(raw_time) >= 5 else f"+{idx}h"  # Extract HH:MM
    code_i = int(hourly_code[idx] or code) if idx < len(hourly_code) else code
    hourly_out.append({
        "hourOffset": int(idx),
        "hourLabel": hour_label,      # e.g., "15:00"
        "tempF": float(hourly_temp[idx] or 0.0),
        "windMph": float(hourly_wind[idx] or 0.0),
        "precipChance": int(hourly_precip[idx] or 0),
        "condition": code_map.get(code_i, f"code {code_i}")
    })
```

**Hourly Point Structure:**
- `hourOffset`: Index 0-11
- `hourLabel`: Time string (e.g., "15:00")
- `tempF`: Temperature
- `windMph`: Wind speed
- `precipChance`: 0-100
- `condition`: Text condition

#### 3f. Sun Time Extraction (Lines 2477-2500)
```python
def _parse_hour(s: str) -> int:
    try:
        return int(str(s or "")[11:13])  # Extract hour from "2026-02-25T15:00"
    except (ValueError, IndexError):
        return -1

daily_sunrise = (daily.get("sunrise") or [])
daily_sunset  = (daily.get("sunset")  or [])
local_hour    = _parse_hour(str(current.get("time", "") or ""))
sunrise_hour  = _parse_hour(str(daily_sunrise[0]) if daily_sunrise else "")
sunset_hour   = _parse_hour(str(daily_sunset[0])  if daily_sunset  else "")

return {
    "ok": True,
    "source": "open-meteo",
    "location": f"{name}{(', ' + country) if country else ''}",
    "temperatureF": float(current.get("temperature_2m", 0.0) or 0.0),
    "windMph": float(current.get("wind_speed_10m", 0.0) or 0.0),
    "condition": code_map.get(code, f"code {code}" if code >= 0 else "unknown"),
    "hourly": hourly_out,                  # List of 12 hourly points
    "localHour": local_hour,               # Current hour at location
    "sunriseHour": sunrise_hour if sunrise_hour >= 0 else 6,
    "sunsetHour": sunset_hour  if sunset_hour  >= 0 else 19,
}
```

**Snapshot Return Structure:**
- `ok`: True if successful
- `source`: "open-meteo"
- `location`: Display name (city, country)
- `temperatureF`: Current temp
- `windMph`: Current wind
- `condition`: Text description
- `hourly`: List of 12 hourly dicts
- `localHour`: Hour at location (-1 if unknown)
- `sunriseHour`: Hour of sunrise (6-19)
- `sunsetHour`: Hour of sunset (6-19)

#### 3g. Exception Handling (Lines 2502-2508)
```python
except Exception:
    # Never hard-fail on provider issues
    fallback = weather_fallback_snapshot(query)
    fallback["source"] = "fallback"
    fallback["degradedFrom"] = "open-meteo"
    return fallback  # Returns mock data if API fails
```

---

### 4. Weather Forecast Operation Handler: `if kind == "weather_forecast"`
**Lines:** 29230-29287

**Purpose:** Entry point in `/api/operation` POST handler

```python
if kind == "weather_forecast":
    # Step 1: Normalize location (handles __current__ → GPS coords)
    location = normalize_weather_location(str(payload.get("location", "")))
    
    # Step 2: Fetch weather snapshot
    snapshot = weather_read_snapshot(location)
    
    # Step 3: Error handling
    if not bool(snapshot.get("ok", False)):
        return {
            "ok": False,
            "message": f"Weather unavailable for {location}.",
            "previewLines": [
                f"location: {location}",
                f"source: {str(snapshot.get('source', 'unknown'))}",
                f"error: {str(snapshot.get('error', 'provider unavailable'))}",
            ],
        }
    
    # Step 4: Extract fields from snapshot
    temperature = float(snapshot.get("temperatureF", 0.0) or 0.0)
    wind = float(snapshot.get("windMph", 0.0) or 0.0)
    condition = str(snapshot.get("condition", "unknown"))
    source = str(snapshot.get("source", "fallback"))
    resolved = str(snapshot.get("location", location))
    hourly = snapshot.get("hourly", [])
    
    # Step 5: Validate and repackage hourly data (max 12 items)
    hourly_data: list[dict[str, Any]] = []
    for item in hourly[:12]:
        if not isinstance(item, dict):
            continue
        hourly_data.append({
            "hourOffset": int(item.get("hourOffset", 0) or 0),
            "hourLabel": str(item.get("hourLabel", ""))[:16],
            "tempF": float(item.get("tempF", 0.0) or 0.0),
            "windMph": float(item.get("windMph", 0.0) or 0.0),
            "precipChance": int(item.get("precipChance", 0) or 0),
            "condition": str(item.get("condition", condition))[:40],
        })
    
    # Step 6: Log event to graph
    graph_add_event(graph, "weather_forecast", {"location": resolved[:80], "source": source})
    
    # Step 7: Build preview lines for UI
    lines = [
        f"location: {resolved}",
        f"condition: {condition}",
        f"temperature: {temperature:.1f}F",
        f"wind: {wind:.1f} mph",
        f"source: {source}",
    ]
    
    # Step 8: Create sourceTarget link
    weather_url = f"https://www.google.com/search?q=weather+{quote_plus(resolved)}"
    source_target = {"label": f"Open forecast for {resolved}", "url": weather_url, "mode": "assist"}
    lines.append(f"direct: {source_target['label']} | {source_target['url']}")
    
    # Step 9: Return response
    return {
        "ok": True,
        "message": f"Weather ready for {resolved}",
        "previewLines": lines[:10],
        "data": {
            "location": resolved,
            "condition": condition,
            "temperatureF": float(temperature),
            "windMph": float(wind),
            "source": source,
            "forecast": hourly_data,                    # 12 hourly points
            "sourceTarget": source_target,              # Link object
        },
    }
```

**Response Structure:**
```json
{
  "ok": true,
  "message": "Weather ready for <location>",
  "previewLines": [
    "location: <city>, <country>",
    "condition: <text>",
    "temperature: <float>F",
    "wind: <float> mph",
    "source: open-meteo"
  ],
  "data": {
    "location": "<city>, <country>",
    "condition": "<text>",
    "temperatureF": <float>,
    "windMph": <float>,
    "source": "open-meteo",
    "forecast": [
      {
        "hourOffset": 0-11,
        "hourLabel": "<HH:MM>",
        "tempF": <float>,
        "windMph": <float>,
        "precipChance": 0-100,
        "condition": "<text>"
      },
      // ... up to 12 items
    ],
    "sourceTarget": {
      "label": "Open forecast for <location>",
      "url": "https://www.google.com/search?q=weather+<encoded>",
      "mode": "assist"
    }
  }
}
```

---

## FRONTEND (app.js) - 5,480 lines total

### 5. Scene Intent Building: `buildCoreSurface()`
**Lines:** 1983-2200+

**Purpose:** Converts latest operation result into UI scene core

#### 5a. Weather Detection & Core Construction (Lines 2001-2020)
```javascript
if (latest.op === 'weather_forecast') {
    // Extract data from operation response
    const weatherData = (latest.data && typeof latest.data === 'object') ? latest.data : {};
    const info = this.parsePreviewMap(latest.previewLines);
    
    // Extract fields
    const temp = String(weatherData.temperatureF || info.temperature || '').replace(/\s*f$/i, '').trim();
    const condition = String(weatherData.condition || info.condition || '').trim();
    const location = String(weatherData.location || info.location || '').trim();
    const wind = String(weatherData.windMph || info.wind || '').trim();
    const source = String(weatherData.source || info.source || '').trim();
    
    // Build headline
    const headline = temp && condition ? `${temp}F, ${condition}` : (latest.message || 'Weather');
    const summaryParts = [location, wind, source ? `source: ${source}` : ''].filter(Boolean);
    
    // Theme based on condition
    const lower = condition.toLowerCase();
    const theme = lower.includes('rain') || lower.includes('storm')
        ? 'theme-rain'
        : lower.includes('snow')
            ? 'theme-snow'
            : lower.includes('sun') || lower.includes('clear')
                ? 'theme-sun'
                : 'theme-cloud';
    
    // Merge all data
    const mergedInfo = { ...info, ...weatherData, forecast: Array.isArray(weatherData.forecast) ? weatherData.forecast : [] };
    
    // RETURN CORE OBJECT
    return {
        headline,                           // e.g., "62F, partly cloudy"
        summary: summaryParts.join(' | '), // e.g., "New York, NY | 8.3 mph | source: open-meteo"
        variant: 'result',
        kind: 'weather',                    // KEY: identifies this as weather scene
        theme,                              // 'theme-rain', 'theme-snow', 'theme-sun', or 'theme-cloud'
        info: mergedInfo                    // Full weather data
    };
}
```

**Core Object Structure (weather kind):**
```javascript
{
  headline: "<temp>F, <condition>",
  summary: "<location> | <wind> mph | source: <source>",
  variant: "result",
  kind: "weather",
  theme: "theme-rain" | "theme-snow" | "theme-sun" | "theme-cloud",
  info: {
    location: "<city>, <country>",
    condition: "<text>",
    temperatureF: <float>,
    windMph: <float>,
    source: "open-meteo",
    forecast: [{ hourOffset, hourLabel, tempF, windMph, precipChance, condition }, ...],
    sourceTarget: { label, url, mode }
  }
}
```

---

### 6. Scene HTML Rendering: `buildSceneHtml()`
**Lines:** 2476-2534

**Purpose:** Converts weather core into HTML with canvas and forecast cards

```javascript
if (core.kind === 'weather') {
    const info = core.info || {};
    const condition = String(info.condition || '').toLowerCase();
    const location = String(info.location || '').trim() || String(this.state.session.lastIntent || '').trim();
    
    // Build forecast cards (visual strip)
    const forecastItems = this.buildWeatherForecastPoints(info).slice(0, 5);  // Get first 5 points
    const forecastStrip = forecastItems.map((p) => {
        const precip = Number(p.precip || 0);
        const wet = precip >= 45 ? 'wet' : precip >= 20 ? 'mixed' : 'dry';
        const cardIcon = this.conditionIcon(p.condition);
        return `
            <div class="weather-forecast-card ${wet}">
                <div class="wfc-time">${escapeHtml(String(p.hour || ''))}</div>
                <div class="wfc-icon">${cardIcon}</div>
                <div class="wfc-temp">${escapeHtml(String(Math.round(Number(p.temp || 0))))}F</div>
                <div class="wfc-bar"><div class="wfc-bar-fill" style="width:${Math.min(100, precip)}%"></div></div>
            </div>
        `;
    }).join('');
    
    // Extract numeric values
    const temperature = Number(info.temperatureF || String(info.temperature || '').replace(/[^0-9.-]/g, '')) || 60;
    const windMph = Number(info.windMph || 0);
    const weatherTarget = (info.sourceTarget && typeof info.sourceTarget === 'object') ? info.sourceTarget : null;
    const weatherTargetUrl = String(weatherTarget?.url || '').trim();
    const weatherTargetLabel = String(weatherTarget?.label || 'open weather source').trim();
    
    // Return HTML structure
    return `
        <div class="scene scene-weather ${escapeAttr(core.theme || '')}">
            <div class="weather-hero-tint"></div>
            <canvas
                class="scene-canvas weather-canvas"
                data-scene="weather"
                data-condition="${escapeAttr(condition)}"
                data-temp="${escapeAttr(String(temperature))}"
                data-wind="${escapeAttr(String(windMph))}"
                data-hour="${escapeAttr(String(info.localHour ?? -1))}"
                data-sunrise="${escapeAttr(String(info.sunriseHour ?? 6))}"
                data-sunset="${escapeAttr(String(info.sunsetHour ?? 19))}"
                data-terrain="${escapeAttr(this.locationTerrain(location))}"
            ></canvas>
            
            <div class="weather-radar" aria-hidden="true">
                <div class="radar-ring ring-1"></div>
                <div class="radar-ring ring-2"></div>
                <div class="radar-ring ring-3"></div>
                <div class="radar-sweep"></div>
            </div>
            
            <div class="scene-orb orb-a"></div>
            <div class="scene-orb orb-b"></div>
            <div class="scene-grid"></div>
            
            <div class="weather-hero">
                <div class="weather-hero-temp">${escapeHtml(String(Math.round(temperature)))}°</div>
                <div class="weather-hero-cond">${escapeHtml(condition)}</div>
                <div class="weather-hero-meta">
                    <span>${escapeHtml(String(Math.round(windMph)))} mph</span>
                    <span class="wh-sep">·</span>
                    <span>${escapeHtml(location)}</span>
                    ${weatherTargetUrl ? `<a class="wh-link" href="${escapeAttr(weatherTargetUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(weatherTargetLabel)}</a>` : ''}
                </div>
            </div>
            
            <div class="weather-forecast-strip">${forecastStrip}</div>
        </div>
    `;
}
```

**Canvas Data Attributes** (used by renderer):
- `data-scene="weather"`: Type identifier
- `data-condition`: Lowercase condition string
- `data-temp`: Numeric temperature
- `data-wind`: Numeric wind speed
- `data-hour`: Local hour (or -1)
- `data-sunrise`: Sunrise hour
- `data-sunset`: Sunset hour
- `data-terrain`: Terrain type

---

### 7. Scene Renderer Setup: `makeSceneRenderer()`
**Lines:** 3154-3160

**Purpose:** Initializes canvas animation renderer based on scene type

```javascript
if (!canvas) return;
const scene = String(canvas.dataset.scene || '').trim();
if (scene === 'weather') {
    this._sceneRenderer = this.makeWeatherRenderer(canvas);  // Create weather-specific renderer
} else if (scene === 'shopping') {
    this._sceneRenderer = this.makeShoppingRenderer(canvas);
} else if (scene === 'tasks') {
    // ... other scene types
}
```

---

### 8. Weather Renderer: `makeWeatherRenderer()`
**Lines:** 3320-3500+ (extensive animation logic)

**Purpose:** Creates WebGL/Canvas animation for weather visualization

#### 8a. Initialization (Lines 3320-3362)
```javascript
makeWeatherRenderer(canvas) {
    const condition = String(canvas.dataset.condition || '').toLowerCase();
    const temp      = Number(canvas.dataset.temp    || 62);
    const wind      = Number(canvas.dataset.wind    || 0);
    const hour      = Number(canvas.dataset.hour    ?? -1);
    const sunrise   = Number(canvas.dataset.sunrise ?? 6);
    const sunset    = Number(canvas.dataset.sunset  ?? 19);
    const terrain   = String(canvas.dataset.terrain || 'hills');

    // Condition detection
    const isRain  = condition.includes('rain') || condition.includes('drizzle') || condition.includes('shower');
    const isStorm = condition.includes('storm') || condition.includes('thunder');
    const isSnow  = condition.includes('snow') || condition.includes('sleet') || condition.includes('ice');
    const isSun   = condition.includes('sun') || condition.includes('clear');
    const isFog   = condition.includes('fog') || condition.includes('haze') || condition.includes('mist');

    // Time-of-day calculation
    const localHour = hour >= 0 ? hour : new Date().getHours();
    const isNight   = localHour < sunrise - 1 || localHour >= sunset + 1;
    const isDawn    = !isNight && localHour >= sunrise - 1 && localHour < sunrise + 1;
    const isDusk    = !isNight && localHour >= sunset - 1 && localHour < sunset + 1.5;
    const isGolden  = !isNight && !isDusk && localHour >= sunset - 2.5 && localHour < sunset - 1;

    // Seeded star field (deterministic, not random)
    const stars = Array.from({ length: 80 }, (_, i) => ({
        x: ((i * 1973 + 83) % 9973) / 9973,
        y: ((i * 2741 + 17) % 9871) / 9871 * 0.72,
        r: 0.5 + ((i * 137) % 10) / 10 * 1.2,
        twinkle: (i * 0.17) % (Math.PI * 2),
    }));

    // Temperature warmth scale
    const warmth = Math.max(0, Math.min(1, (temp - 32) / 68));  // 0=freezing, 1=hot
    
    // Rain angle based on wind
    const rainAngle = Math.min(0.62, wind * 0.031);  // 0.62 rad = ~35°

    // Angled rain streaks
    const droplets = Array.from({ length: 130 }, (_, i) => ({
        x: (i * 37.3) % 1,
        y: ((i * 83.7) % 991) / 991,
        v: 0.38 + ((i * 17) % 100) / 100,
        len: 0.016 + ((i * 11) % 100) / 5500,
    }));

    // Parallax snow (3 layers: near/mid/far)
    const snowLayers = [
        Array.from({ length: 28 }, (_, i) => ({ x: (i * 0.618) % 1, y: (i * 0.38) % 1, sz: 3.5 + (i % 3) * 0.8, sp: 0.0011, dr: i * 0.28 })),
        Array.from({ length: 48 }, (_, i) => ({ x: (i * 0.382) % 1, y: (i * 0.23) % 1, sz: 2.0,                   sp: 0.00065, dr: i * 0.18 })),
        Array.from({ length: 72 }, (_, i) => ({ x: (i * 0.236) % 1, y: (i * 0.17) % 1, sz: 1.1,                   sp: 0.00032, dr: i * 0.10 })),
    ];
    
    // ... Animation renderer logic continues (drawing, animation loops, etc.)
    
    return {
        // Animation object with render(), update(), etc.
    };
}
```

**Key Canvas Attributes Used:**
- `condition`: Determines which weather effect (rain/snow/sun/etc.)
- `temp`: Used for warmth scale (affects colors)
- `wind`: Used for rain angle
- `hour`, `sunrise`, `sunset`: Determine day/night/dawn/dusk effects
- `terrain`: Terrain type for background

---

### 9. Forecast Points Builder: `buildWeatherForecastPoints()`
**Lines:** 4297-4322

**Purpose:** Converts hourly forecast data into visual points OR generates mock points if live data missing

```javascript
buildWeatherForecastPoints(info) {
    // Get live forecast from backend data
    const live = Array.isArray(info?.forecast) ? info.forecast : [];
    
    // PATH A: Use live data if available (4+ points)
    if (live.length >= 4) {
        return live.slice(0, 8).map((item, idx) => ({
            hour: String(item.hourLabel || `+${idx}h`),
            temp: Number(item.tempF || 0),
            precip: Number(item.precipChance || 0),
            wind: Number(item.windMph || 0),
        }));
    }
    
    // PATH B: Generate synthetic points if no live data
    const location = String(info.location || 'weather').trim();
    const condition = String(info.condition || '').trim().toLowerCase();
    const baseTemp = Number(String(info.temperature || '').replace(/[^0-9.-]/g, '')) || 60;
    const seed = this.weatherSeed(`${location}|${condition}|${baseTemp}`);  // Deterministic seed
    
    // Apply condition bias
    const conditionBias = condition.includes('rain') ? -3 
                        : condition.includes('snow') ? -6 
                        : condition.includes('sun') || condition.includes('clear') ? 4 
                        : 0;
    
    // Generate 8 synthetic points
    const points = [];
    for (let i = 0; i < 8; i += 1) {
        const jitter = ((seed >> (i * 4)) & 0xf) - 7;  // Pseudo-random variation
        const wave = Math.sin((i / 5) * Math.PI) * 3;   // Sine wave pattern
        const temp = Math.round(baseTemp + conditionBias + wave + jitter * 0.35);
        const precip = Math.max(0, Math.min(100, (condition.includes('rain') ? 40 : 16) + jitter * 3 + i * 2));
        const wind = Math.max(0, 4 + (jitter * 0.4) + (i * 0.2));
        points.push({ hour: `${i * 2}h`, temp, precip, wind });
    }
    return points;
}
```

**Point Structure Returned:**
```javascript
{
  hour: "<label>",         // "15:00" (live) or "+0h", "+2h", ... (synthetic)
  temp: <number>,          // Temperature in F
  precip: 0-100,           // Precipitation probability
  wind: <number>,          // Wind speed mph
}
```

**Key Decision:**
- **Live data (≥4 points):** Use backend hourly forecast directly, slice to 8
- **No live data:** Generate deterministic mock points based on location/condition/temp seed

---

### 10. Intent to Scene Domain Mapping: `sceneDomainToIntent()`
**Lines:** 1453-1475

**Purpose:** Converts scene domain ID to natural language intent for dispatching

```javascript
sceneDomainToIntent(domain) {
    const locationHint = this.resolveIntentLocationHint();
    // For weather: GPS coords always win
    const weatherHint = String(this.state.session.locationHint || '').trim() || locationHint;
    console.log('[weather-debug] sceneDomainToIntent domain=', domain, 'locationHint=', locationHint, 'session.locationHint=', this.state.session.locationHint, 'weatherHint=', weatherHint, '_fromGPS=', this.state.session._locationHintFromGPS);
    
    const byDomain = {
        tasks: 'show tasks',
        expenses: 'show expenses',
        notes: 'show notes',
        graph: 'show graph summary',
        files: 'show files',
        weather: weatherHint ? `show weather in ${weatherHint}` : "what's the weather where i am",  // KEY: Uses GPS hint
        location: 'where am i',
        shopping: 'show me running shoes',
        webdeck: 'open example.com',
        social: 'show my social feed',
        banking: 'show account balances',
        contacts: 'show contacts',
        telephony: 'show call status',
        generic: 'show me what i can do'
    };
    return byDomain[String(domain || 'generic')] || byDomain.generic;
}
```

**Weather Intent Generation:**
- If `locationHint` present: `"show weather in <hint>"` (e.g., "show weather in 40.7128,-74.0060")
- If no hint: `"what's the weather where i am"` (triggers auto-location)

---

### 11. Scene Domain Switching: `switchToSceneDomain()`
**Lines:** 1505-1536

**Purpose:** Handles scene domain activation with weather-specific logic

```javascript
async switchToSceneDomain(domain) {
    const target = String(domain || '').trim().toLowerCase();
    if (!target) return;
    
    // Weather is time-sensitive — never restore stale history
    if (target !== 'weather') {
        // Check history for matching domain
        let matchIndex = -1;
        for (let i = this.state.history.length - 1; i >= 0; i -= 1) {
            const entry = this.state.history[i];
            const entryDomain = this.inferHistoryEntryDomain(entry);
            if (entryDomain === target) { matchIndex = i; break; }
        }
        if (matchIndex >= 0) {
            this.restoreFromHistory(matchIndex);  // Restore from history
            return;
        }
    }
    
    // WEATHER-SPECIFIC LOGIC (Lines 1521-1533)
    if (target === 'weather') {
        const hint = String(this.state.session.locationHint || '').trim();
        const isCoords = /^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$/.test(hint);  // Validate lat,lon format
        console.log('[weather-debug] switchToSceneDomain weather check: hint=', hint, 'isCoords=', isCoords, '_fromGPS=', this.state.session._locationHintFromGPS);
        
        if (!hint || !isCoords) {
            // Missing or invalid coordinates — clear and re-detect
            this.state.session.locationHint = '';
            this.state.session._locationHintFromGPS = false;
            console.log('[weather-debug] clearing stale hint, re-detecting GPS...');
            await this.primeRelativeLocationContext().catch(() => {});  // Auto-detect GPS
            console.log('[weather-debug] after GPS detect: session.locationHint=', this.state.session.locationHint);
        }
    }
    
    // Dispatch intent
    this.handleIntent(this.sceneDomainToIntent(target));
}
```

**Weather-Specific Logic:**
1. Checks `session.locationHint` is in `lat,lon` coordinate format
2. If missing or not coordinates: clears it and calls `primeRelativeLocationContext()`
3. Guarantees fresh GPS coordinates before dispatching

---

## Data Flow Diagram

```
┌─ USER INTENT ─────────────────────────────────────────────┐
│ "weather in boston" / "what's the weather where i am"      │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
       ┌───────────────────────────────────────┐
       │ parse_semantic_command() [line 16995]  │
       │ Detects weather signal                 │
       │ Extracts location / location hint      │
       └───────────────┬───────────────────────┘
                       │
                       ▼ (Returns payload)
       ┌───────────────────────────────────────┐
       │ {"type": "weather_forecast",          │
       │  "domain": "weather",                 │
       │  "payload": {"location": "..."}}      │
       └───────────────┬───────────────────────┘
                       │
         ┌─────────────┴─────────────┐
         │                           │
         ▼                           ▼
 (dispatch to /api/operation)  (UI: sceneDomainToIntent)
         │                           │
         │                           ▼
         │              "show weather in <location>"
         │
         ▼
 ┌─────────────────────────────────────────────┐
 │ Operation Handler [line 29230]              │
 │ normalize_weather_location()                │
 │ - Handles "__current__" → GPS location      │
 └─────────────────┬───────────────────────────┘
                   │
                   ▼
 ┌─────────────────────────────────────────────┐
 │ weather_read_snapshot() [line 2349]         │
 │ ┌─────────────────────────────────────────┐ │
 │ │ Option A: lat,lon → Nominatim reverse   │ │
 │ │ Option B: city name → open-meteo geo    │ │
 │ └──────────────────┬──────────────────────┘ │
 │                    │                         │
 │                    ▼                         │
 │ ┌─────────────────────────────────────────┐ │
 │ │ open-meteo API forecast call [2404]     │ │
 │ │ Request: lat,lon,current,hourly,daily   │ │
 │ │ Returns: weather code, temps, wind, sun │ │
 │ └──────────────────┬──────────────────────┘ │
 │                    │                         │
 │                    ▼                         │
 │ ┌─────────────────────────────────────────┐ │
 │ │ Process response [2423-2500]            │ │
 │ │ - Map weather codes → text descriptions │ │
 │ │ - Extract 12-hour forecast              │ │
 │ │ - Parse sunrise/sunset times            │ │
 │ └──────────────────┬──────────────────────┘ │
 │                    │                         │
 │                    ▼                         │
 │ ┌─────────────────────────────────────────┐ │
 │ │ Return snapshot {                       │ │
 │ │   ok, source, location,                 │ │
 │ │   temperatureF, windMph, condition,     │ │
 │ │   hourly: [{...}, ...],                 │ │
 │ │   localHour, sunriseHour, sunsetHour    │ │
 │ │ }                                       │ │
 │ └──────────────────┬──────────────────────┘ │
 └─────────────────────┬───────────────────────┘
                       │
                       ▼
 ┌─────────────────────────────────────────────┐
 │ Operation Handler returns [line 29274]      │
 │ {                                           │
 │   "ok": true,                               │
 │   "data": {                                 │
 │     location, condition, temperatureF,      │
 │     windMph, source,                        │
 │     forecast: [...],                        │
 │     sourceTarget: {...}                     │
 │   },                                        │
 │   "previewLines": [...]                     │
 │ }                                           │
 └─────────────────────┬───────────────────────┘
                       │
         ┌─────────────┴─────────────┐
         │                           │
    (turn response)          (stored in execution)
         │                           │
         ▼                           ▼
 ┌──────────────────┐   ┌──────────────────────────────┐
 │ UI receives      │   │ buildCoreSurface() [1983]    │
 │ (JSON data)      │   │ Converts latest.op result to │
 │                  │   │ scene core object            │
 │                  │   │                              │
 │                  │   │ core = {                     │
 │                  │   │   kind: 'weather',           │
 │                  │   │   theme: 'theme-rain/sun',   │
 │                  │   │   info: {...}                │
 │                  │   │ }                            │
 └──────────────────┘   └─────────────┬────────────────┘
                                      │
                                      ▼
                        ┌──────────────────────────────┐
                        │ buildSceneHtml() [2476]      │
                        │ Renders core.kind == weather │
                        │ Creates:                     │
                        │ - Canvas with data attrs     │
                        │ - Forecast cards (strip)     │
                        │ - Weather hero display       │
                        └─────────────┬────────────────┘
                                      │
                                      ▼
                        ┌──────────────────────────────┐
                        │ makeSceneRenderer() [3154]   │
                        │ Attaches makeWeatherRenderer │
                        │ to canvas                    │
                        └─────────────┬────────────────┘
                                      │
                                      ▼
                        ┌──────────────────────────────┐
                        │ makeWeatherRenderer() [3320] │
                        │ Reads canvas.dataset attrs   │
                        │ Creates animation:           │
                        │ - Rain/snow/clear effects    │
                        │ - Time-of-day sky            │
                        │ - Terrain + stars            │
                        │ - Animated weather           │
                        └──────────────────────────────┘
                                      
                        (Forecast points used in strip)
                                      │
                                      ▼
                        ┌──────────────────────────────┐
                        │ buildWeatherForecastPoints() │
                        │ [4297]                       │
                        │ Live data path (≥4 points):  │
                        │  → Use backend forecast      │
                        │ Fallback path:               │
                        │  → Generate synthetic points │
                        │    using seed + condition    │
                        └──────────────────────────────┘
```

---

## Critical Decision Points

### Backend Payload Structure
```python
# Intent → Operation
{"type": "weather_forecast", "domain": "weather", "payload": {"location": str}}

# Operation → Response
{
  "ok": bool,
  "data": {
    "location": str,
    "condition": str,
    "temperatureF": float,
    "windMph": float,
    "source": str,
    "forecast": [{"hourOffset": int, "hourLabel": str, "tempF": float, "windMph": float, "precipChance": int, "condition": str}, ...],
    "sourceTarget": {"label": str, "url": str, "mode": str}
  },
  "previewLines": [str, ...]
}
```

### Frontend Scene Core Structure
```javascript
{
  kind: "weather",  // KEY: identifies as weather scene
  theme: "theme-rain|theme-snow|theme-sun|theme-cloud",  // CSS class based on condition
  headline: "<temp>F, <condition>",
  summary: "<location> | <wind> mph | source: <source>",
  variant: "result",
  info: {
    location, condition, temperatureF, windMph, source, forecast, sourceTarget, ...
  }
}
```

### Canvas Data Attributes
```html
<canvas
  data-scene="weather"
  data-condition="<condition>"
  data-temp="<tempF>"
  data-wind="<windMph>"
  data-hour="<localHour>"
  data-sunrise="<sunriseHour>"
  data-sunset="<sunsetHour>"
  data-terrain="<terrain>"
></canvas>
```

### Forecast Point Structure
```javascript
{
  hour: str,      // "15:00" or "+0h"
  temp: float,    // Temperature F
  precip: 0-100,  // Precipitation probability
  wind: float     // Wind mph
}
```

---

## Environment Variables (main.py)
- `CONNECTOR_PROVIDER_MODE`: "auto" | "mock" (line 44)
- `GENOMEUI_STORE_PATH`: Session storage path (line 40)
- `TURN_LATENCY_BUDGET_MS`: Timeout budget (line 49)

## API Endpoints
- **Backend Intent Parsing:** POST `/api/operation` (operation handler)
- **Open-Meteo Geocoding:** GET `https://geocoding-api.open-meteo.com/v1/search`
- **Open-Meteo Forecast:** GET `https://api.open-meteo.com/v1/forecast`
- **Nominatim Reverse Geocoding:** GET `https://nominatim.openstreetmap.org/reverse`

