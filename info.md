# Health Auto Import

A companion to the [**Health Auto Export**](https://www.healthyapps.dev/) iOS app by [HealthyApps](https://healthyapps.dev/). Connects to HAE's built-in TCP/MCP server (Premium feature) and turns the data it returns into **persistent, auto-discovered Home Assistant sensors** — ECG, heart rate, HR notifications, medications, vitals, workouts.

Pull-based companion to HAE's MQTT / REST API / Home Assistant Sync options. Auto-discovers which tools and metrics your device exposes, formats each as a first-class HA entity with proper device class and unit, and adapts its polling interval per sensor based on how often the source actually updates.

Add via **Settings → Devices & Services → Add Integration → Health Auto Import** and enter the IP/port shown on HAE's Server screen.

Not affiliated with HealthyApps — please support the upstream team by buying HAE Premium. See [the README](https://github.com/leithma/health-auto-import) for the full walkthrough.
