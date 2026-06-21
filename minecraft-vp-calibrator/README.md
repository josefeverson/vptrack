# VPTrack Calibrator Mod

Client-side Fabric mod for Minecraft 1.21.1. It watches for the local player sending `/voteparty`, then scans the visible response text for a dynamic `x/120` count and runs a local calibration command.

It does not register server commands, send hidden chat messages, automate voting, solve CAPTCHAs, or send any extra packets to the server. It only observes the command you manually sent and the chat/actionbar response your client receives.

## Local Config

On first run it creates:

```text
config/vptrack-calibrator.json
```

Set `calibrationCommand` to a local command where `{count}` is replaced with the parsed numerator:

```json
{
  "enabled": true,
  "listenWindowSeconds": 12,
  "showClientConfirmation": true,
  "calibrationCommand": [
    "/path/to/python",
    "/path/to/vp_tracker.py",
    "--config",
    "/path/to/config.json",
    "--calibrate",
    "{count}"
  ]
}
```

The parser accepts any message containing Vote Party text and a count like `47/120`.

The config is reloaded each time you send `/voteparty`, so changing the command or patterns only requires re-running `/voteparty`. Replacing the mod jar itself still requires a Minecraft restart.
