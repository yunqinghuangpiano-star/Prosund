#!/usr/bin/env python3
"""Open every public route from the address bar, then from the menu.

A route passes only when its own heading is on screen and the document
starts at the top (or at the requested section). Leaked fallback sections
outside #ps-fallback fail the run.
"""
import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

CHROME = "google-chrome"
INDEX = Path(__file__).resolve().parents[1].joinpath("index.html").as_uri()

EXPECT = {
    "/home/banner": "射频微波",
    "/products": "测试测量仪表",
    "/products/14": "SP850B",
    "/products/3": "SP809B",
    "/products/8": "SP240",
    "/products/9": "SP206V",
    "/products/15": "SP950P",
    "/products/7": "SP940N",
    "/products/951": "SP951SH",
    "/applications": "解决方案",
    "/solutions/satcom": "卫星通信测试解决方案",
    "/support": "技术支持",
    "/about": "关于普尚",
    "/research": "从研发到生产",
    "/news/sp2110v": "SP2110V",
}

DIRECT = ["/", "/home/banner"] + [route for route in EXPECT if route != "/home/banner"]
DIRECT.append("/about~careers")


class Cdp:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def call(self, method, params=None):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"])[:300])
                return msg["result"]


PROBE = r"""
(() => {
  const h1 = document.querySelector("[data-ps-live] h1");
  const leaked = [...document.querySelectorAll("body > section")].map(s => s.id || s.className);
  const anchor = %s;
  const target = anchor ? document.getElementById(anchor) : null;
  const box = target ? target.getBoundingClientRect() : null;
  return JSON.stringify({
    hash: location.hash,
    y: Math.round(window.scrollY || 0),
    h1: h1 ? h1.innerText.replace(/\s+/g, " ").trim() : "",
    h1Top: h1 ? Math.round(h1.getBoundingClientRect().top) : null,
    leaked,
    anchorTop: box ? Math.round(box.top) : null,
    atEnd: (window.scrollY || 0) + window.innerHeight >= document.documentElement.scrollHeight - 4
  });
})()
"""


def start_chrome(port):
    subprocess.Popen(
        [
            CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
            f"--remote-debugging-port={port}", f"--user-data-dir=/tmp/chrome-routes-{port}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.3)
            return
        except Exception:
            time.sleep(0.1)
    raise SystemExit("Chrome did not start")


def page_ws(port):
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
    return next(p["webSocketDebuggerUrl"] for p in pages if p.get("type") == "page")


def judge(label, info, route, anchor):
    data = json.loads(info)
    problems = []
    if data["leaked"]:
        problems.append("leaked " + ",".join(data["leaked"][:4]))
    expected = EXPECT["/about" if route == "/about" else route if route in EXPECT else "/home/banner"]
    if route in ("/", "/home/banner"):
        expected = EXPECT["/home/banner"]
    if expected not in data["h1"]:
        problems.append(f"h1 {data['h1'][:40]!r} != {expected}")
    if anchor:
        top = data["anchorTop"]
        # 224px is the sticky header plus scroll-margin. A section at the
        # end of a page cannot scroll any higher than the document allows.
        if top is None or top < -8 or (top > 320 and not data.get("atEnd")):
            problems.append(f"anchor {anchor} top={top}")
        elif top > 320 and (top < 0 or top > 900):
            problems.append(f"anchor {anchor} not visible at {top}")
    else:
        if data["y"] > 2:
            problems.append(f"scroll {data['y']}")
        if data["h1Top"] is None or data["h1Top"] < 0 or data["h1Top"] > 720:
            problems.append(f"h1 offscreen {data['h1Top']}")
    if problems:
        print(f"FAIL {label}: {'; '.join(problems)} hash={data['hash']}")
        return False
    print(f"ok   {label}: {data['h1'][:28]}")
    return True


async def direct(cdp, width, height, token):
    await cdp.call("Emulation.setDeviceMetricsOverride", {
        "width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 700,
    })
    passed = True
    for i, spec in enumerate(DIRECT):
        route, _, anchor = spec.partition("~")
        url = f"{INDEX}?direct={token}-{width}-{i}#{route if route != '/' else ''}{'~' + anchor if anchor else ''}"
        if spec == "/":
            url = f"{INDEX}?direct={token}-{width}-root#/"
        await cdp.call("Page.navigate", {"url": url})
        await asyncio.sleep(0.85)
        info = (await cdp.call("Runtime.evaluate", {
            "expression": PROBE % json.dumps(anchor), "returnByValue": True,
        }))["result"]["value"]
        passed &= judge(f"direct {width} {spec}", info, "/home/banner" if spec == "/" else route, anchor)
    return passed


MENU = r"""
(() => {
  const root = document.querySelector(%s);
  const seen = new Set();
  const out = [];
  root.querySelectorAll("a[data-route]").forEach(a => {
    const route = a.getAttribute("data-route");
    const anchor = a.getAttribute("data-anchor") || "";
    const key = route + "~" + anchor;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({route, anchor, text: a.textContent.replace(/\s+/g, " ").trim().slice(0, 40)});
  });
  return JSON.stringify(out);
})()
"""

CLICK = r"""
(() => {
  const root = document.querySelector(%s);
  const wantRoute = %s;
  const wantAnchor = %s;
  const link = [...root.querySelectorAll("a[data-route]")].find(a =>
    a.getAttribute("data-route") === wantRoute && (a.getAttribute("data-anchor") || "") === wantAnchor);
  if (!link) return "missing";
  link.click();
  return "clicked";
})()
"""


async def menu(cdp, width, height, token, selector):
    await cdp.call("Emulation.setDeviceMetricsOverride", {
        "width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 700,
    })
    await cdp.call("Page.navigate", {"url": f"{INDEX}?menu={token}-{width}#/home/banner"})
    await asyncio.sleep(0.9)
    links = json.loads((await cdp.call("Runtime.evaluate", {
        "expression": MENU % json.dumps(selector), "returnByValue": True,
    }))["result"]["value"])
    passed = True
    for item in links:
        clicked = (await cdp.call("Runtime.evaluate", {
            "expression": CLICK % (json.dumps(selector), json.dumps(item["route"]), json.dumps(item["anchor"])),
            "returnByValue": True,
        }))["result"]["value"]
        if clicked != "clicked":
            print(f"FAIL menu {width} {item['text']}: link missing")
            passed = False
            continue
        await asyncio.sleep(1.2)
        info = (await cdp.call("Runtime.evaluate", {
            "expression": PROBE % json.dumps(item["anchor"]), "returnByValue": True,
        }))["result"]["value"]
        passed &= judge(f"menu {width} {item['text']}", info, item["route"], item["anchor"])
    return passed


async def main():
    port = 9777
    start_chrome(port)
    passed = True
    async with websockets.connect(page_ws(port), max_size=8_000_000) as ws:
        cdp = Cdp(ws)
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        passed &= await direct(cdp, 1440, 1000, "a")
        passed &= await direct(cdp, 390, 900, "b")
        passed &= await menu(cdp, 1440, 1000, "c", 'nav[aria-label="全站导航"]')
        passed &= await menu(cdp, 390, 900, "d", "nav.menu-tree")
    if not passed:
        print("ROUTE CHECK FAILED")
        return 1
    print("ROUTE CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
