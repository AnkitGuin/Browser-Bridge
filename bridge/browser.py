import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Literal
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, sync_playwright
from playwright.sync_api import TimeoutError as BrowserTimeout
from pydantic import Field, TypeAdapter

from . import config, db
from .contracts import Input
from .errors import BridgeError

VIEWPORT = {"width": 1280, "height": 820}
PORTAL_PATH = re.compile(
    r"^/(?:support(?:/tickets(?:/[\w-]+)?)?|crm(?:/customers(?:/[\w-]+)?)?|inventory(?:/products(?:/[\w-]+)?)?)$"
)
ASSETS = {"/assets/portal.css", "/assets/portal.js"}
SECRET = re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|(?:\+?\d[\s().-]*){9,}", re.I)


def scrub(text):
    return SECRET.sub("[redacted]", text)


class ActionBase(Input):
    observation_id: str | None = None


class Navigate(ActionBase):
    kind: Literal["navigate", "open_tab"]
    route: str = Field(max_length=350)


class TargetAction(ActionBase):
    ref: str


class Click(TargetAction):
    kind: Literal["click"]


class Fill(TargetAction):
    kind: Literal["fill"]
    value: str = Field(max_length=500)


class Select(TargetAction):
    kind: Literal["select_option"]
    option: str = Field(max_length=120)


class Check(TargetAction):
    kind: Literal["check"]
    checked: bool


class Press(ActionBase):
    kind: Literal["press_key"]
    ref: str | None = None
    key: Literal["Escape", "Tab", "ArrowDown", "ArrowUp"]


class Tab(ActionBase):
    kind: Literal["switch_tab", "close_tab"]
    tab: int = Field(ge=0, le=2)


class Scroll(ActionBase):
    kind: Literal["scroll"]
    amount: int = Field(ge=-1200, le=1200)


class Wait(ActionBase):
    kind: Literal["wait_for"]
    ref: str
    timeout_ms: int = Field(ge=1, le=5000)


class Observe(ActionBase):
    kind: Literal["capture_observation"]


ACTION = TypeAdapter(
    Annotated[
        Navigate | Click | Fill | Select | Check | Press | Tab | Scroll | Wait | Observe,
        Field(discriminator="kind"),
    ]
)


@dataclass
class Observation:
    id: str
    url: str
    title: str
    version: str
    tab: int
    timestamp: int
    elements: list[dict] = field(default_factory=list)
    text_excerpt: str = ""

    def public(self):
        return {
            "observation_id": self.id,
            "url": self.url,
            "title": self.title,
            "active_tab_id": self.tab,
            "timestamp_utc": db.iso(self.timestamp),
            "semantic_elements": self.elements,
            "relevant_text_excerpt": self.text_excerpt,
            "loading_state": "ready",
            "navigation_state": "stable",
        }


class LocatorResolver:
    def __init__(self, runtime):
        self.runtime = runtime
        self.refs = {}
        self.observation = None

    def bind(self, locator):
        count = locator.count()
        if count > 1:
            raise BridgeError("TARGET_AMBIGUOUS", "Multiple controls matched")
        if count != 1 or not locator.is_visible() or not locator.is_enabled():
            raise BridgeError("LOCATOR_STALE", "Expected control is missing or unavailable")
        ref = f"e{len(self.refs) + 1}"
        handle = locator.element_handle(timeout=1000)
        self.refs[ref] = handle
        return ref

    def resolve(self, ref, observation_id):
        if (
            not self.observation
            or observation_id != self.observation.id
            or self.observation.version != self.runtime.version()
        ):
            raise BridgeError("LOCATOR_STALE", "Observation no longer matches the page")
        handle = self.refs.get(ref)
        if (
            not handle
            or not handle.evaluate("(e) => e.isConnected")
            or not handle.is_visible()
            or not handle.is_enabled()
        ):
            raise BridgeError("LOCATOR_STALE", "Element was replaced or disabled")
        return handle

    def reset(self, observation=None):
        for handle in self.refs.values():
            handle.dispose()
        self.refs.clear()
        self.observation = observation


def pixel(x, y, width, height):
    if not (0 <= x < 1 and 0 <= y < 1):
        raise BridgeError("LOCATOR_STALE", "Visual target falls outside the viewport")
    return x * width, y * height


@dataclass(frozen=True)
class Target:
    description: str
    names: tuple[str, ...]
    guard: Locator
    role: str = "button"
    visual_only_label: str = ""


class DOM:
    def choose(self, runtime, target, obs):
        locator = target.guard.and_(
            runtime.page.get_by_role(
                target.role, name=re.compile(r"^(?:" + "|".join(map(re.escape, target.names)) + r")$", re.I)
            )
        )
        ref = runtime.resolver.bind(locator)
        return {
            "method": "dom",
            "candidate_count": 1,
            "disagreement": False,
            "ref": ref,
            "observation_id": obs.id,
            "reason": "Unique scoped accessible role and name",
        }


class Vision:
    def choose(self, runtime, target, obs):
        if not runtime.model:
            raise BridgeError("MODEL_ERROR", "Visual grounding needs a multimodal provider")
        if target.guard.count() != 1 or not target.guard.is_visible() or not target.guard.is_enabled():
            raise BridgeError("TARGET_AMBIGUOUS", "Vision needs one authorized actionable control")
        target.guard.scroll_into_view_if_needed()
        screenshot = runtime.screenshot()
        candidates = runtime.model.ground_visual_target(screenshot, target.description)
        matches = []
        labels = {value.casefold() for value in (*target.names, target.visual_only_label) if value}
        for candidate in candidates:
            if candidate.confidence < 0.75 or candidate.label.casefold() not in labels:
                continue
            try:
                x, y = pixel(candidate.x, candidate.y, **VIEWPORT)
            except BridgeError:
                continue
            if target.guard.evaluate(
                "(e,p)=>{const hit=document.elementFromPoint(p.x,p.y);return hit===e||e.contains(hit)}",
                {"x": x, "y": y},
            ):
                matches.append((x, y))
        if len(matches) != 1:
            raise BridgeError(
                "TARGET_AMBIGUOUS", "Visual coordinates did not agree with one authorized control"
            )
        return {
            "method": "vision",
            "candidate_count": len(candidates),
            "disagreement": False,
            "ref": runtime.resolver.bind(target.guard),
            "observation_id": obs.id,
            "point": matches[0],
            "reason": "Screenshot candidate passed authorized-control hit test",
        }


class Hybrid:
    def choose(self, runtime, target, obs):
        try:
            return DOM().choose(runtime, target, obs)
        except BridgeError as exc:
            if not runtime.model:
                raise BridgeError(
                    exc.code, "Semantic target unavailable and visual provider is unconfigured"
                ) from exc
            fresh = runtime.observe()
            selected = Vision().choose(runtime, target, fresh)
            return {
                **selected,
                "method": "hybrid_vision",
                "disagreement": True,
                "reason": "Semantic target unavailable; screenshot and scoped control agreed",
            }


class Scripted:
    def choose(self, runtime, target, obs):
        locator = target.guard.and_(runtime.page.get_by_role(target.role, name=target.names[0], exact=True))
        return {
            "method": "scripted",
            "candidate_count": 1,
            "disagreement": False,
            "ref": runtime.resolver.bind(locator),
            "observation_id": obs.id,
            "reason": "Fixed locator for the base interface",
        }


STRATEGIES = {"dom": DOM(), "vision": Vision(), "hybrid": Hybrid(), "scripted": Scripted()}


class Browser:
    def __init__(self, apps, mode, token, consume, model=None):
        self.apps = apps
        self.mode = mode
        self.token = token
        self.consume = consume
        self.model = model
        self.blocked = 0
        self.pages = []
        self.resolver = LocatorResolver(self)
        self.base = urlsplit(config.BROWSER_URL)
        if (
            self.base.scheme not in {"http", "https"}
            or self.base.hostname not in {"127.0.0.1", "localhost", "app"}
            or self.base.path not in {"", "/"}
            or self.base.query
            or self.base.fragment
            or self.base.username
        ):
            raise BridgeError("POLICY_DENIED", "Browser base must be the local or Compose mock service", 403)

    def allowed(self, url, *, assets=False):
        value = urlsplit(url)
        if value.scheme != self.base.scheme or value.netloc != self.base.netloc or value.fragment:
            return False
        if assets and value.path in ASSETS and not value.query:
            return True
        return bool(PORTAL_PATH.fullmatch(value.path) and value.path.split("/")[1] in self.apps)

    def route_request(self, route):
        request = route.request
        if not self.allowed(request.url, assets=True):
            self.blocked += 1
            route.abort()
            return
        if not request.is_navigation_request():
            route.continue_()
            return
        # Playwright may route only the first request of a redirect chain.
        # Check the response before Chromium can follow Location.
        try:
            response = route.fetch(max_redirects=0, timeout=5500)
        except PlaywrightError:
            route.abort()
            return
        try:
            location = response.headers.get("location")
            if 300 <= response.status < 400 and location and not self.allowed(urljoin(request.url, location)):
                self.blocked += 1
                route.abort()
                return
            route.fulfill(response=response)
        finally:
            response.dispose()

    def __enter__(self):
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(
                executable_path=config.BROWSER_CHROMIUM, args=["--disable-dev-shm-usage"]
            )
            self.context = self.browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=1,
                locale="en-US",
                timezone_id="UTC",
                accept_downloads=False,
                service_workers="block",
            )
            self.context.set_default_timeout(3500)
            self.context.set_default_navigation_timeout(7000)
            self.context.route("**/*", self.route_request)
            self.context.route_web_socket("**/*", lambda ws: ws.close())
            self.context.add_cookies(
                [
                    {
                        "name": config.COOKIE,
                        "value": self.token,
                        "url": config.BROWSER_URL,
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            self.page = self.context.new_page()
            self.pages.append(self.page)
            self.context.on("page", lambda page: page.close() if page.opener() else None)
            self.context.on("dialog", lambda dialog: dialog.dismiss())
        except BaseException:
            self.playwright.stop()
            raise
        return self

    def __exit__(self, *_):
        self.playwright.stop()

    def version(self):
        return self.page.evaluate("() => String(performance.timeOrigin)")

    def check_page(self):
        if not self.allowed(self.page.url):
            raise BridgeError("POLICY_DENIED", "Browser left the approved application scope", 403)
        error = self.page.locator("[data-error]")
        if error.count():
            raise BridgeError(error.get_attribute("data-error"), "Business portal returned an error")
        self.page.locator("main").wait_for(state="visible")

    def open(self, route):
        url = urljoin(config.BROWSER_URL + "/", route)
        if not self.allowed(url):
            self.blocked += 1
            raise BridgeError(
                "POLICY_DENIED", "Navigation is outside the authorized origin or application", 403
            )
        self.consume("navigate")
        self.resolver.reset()
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            self.check_page()
        except BrowserTimeout as exc:
            raise BridgeError("NAVIGATION_TIMEOUT", "Application navigation timed out") from exc

    def screenshot(self):
        self.check_page()
        return self.page.screenshot(
            timeout=3500,
            animations="disabled",
            scale="css",
            mask=[self.page.locator("[data-private], [data-untrusted], input, textarea")],
        )

    def observe(self):
        self.check_page()
        obs = Observation(
            db.uid(), self.page.url, self.page.title(), self.version(), self.pages.index(self.page), db.now()
        )
        self.resolver.reset(obs)
        # Fixed, read-only extraction. Model-supplied selectors or JavaScript never run.
        elements = self.page.locator(
            "main a, main button, main select, main input:not([type=hidden])"
        ).evaluate_all(
            """els => els.filter(e => !e.closest('[data-private],[data-untrusted]') && e.getClientRects().length)
            .slice(0,42).map(e => { const b=e.getBoundingClientRect();return {
                role:e.getAttribute('role') || ({A:'link',BUTTON:'button',SELECT:'combobox',INPUT:'textbox'})[e.tagName],
                name:(e.getAttribute('aria-label') || e.labels?.[0]?.textContent || e.textContent || '').trim().slice(0,110),
                visible:true, enabled:!e.disabled, bounds:[b.x,b.y,b.width,b.height].map(Math.round)
            }})"""
        )
        obs.elements = [{**element, "name": scrub(element["name"])} for element in elements]
        obs.text_excerpt = scrub(self.page.locator("h1").inner_text(timeout=1000))[:250]
        return obs

    def execute(self, value):
        action = ACTION.validate_python(value)
        kind = action.kind
        if kind == "navigate":
            self.open(action.route)
            return
        if kind == "capture_observation":
            return self.observe()
        self.consume(kind)
        if kind == "open_tab":
            if len(self.pages) >= 3:
                raise BridgeError("BUDGET_EXCEEDED", "Tab limit reached")
            self.page = self.context.new_page()
            self.pages.append(self.page)
            self.open(action.route)
        elif kind in {"switch_tab", "close_tab"}:
            self.resolver.reset()
            if action.tab >= len(self.pages) or (kind == "close_tab" and len(self.pages) < 2):
                raise BridgeError("LOCATOR_STALE", "Tab does not exist or cannot be closed")
            if kind == "close_tab":
                self.pages.pop(action.tab).close()
                self.page = self.pages[0]
            else:
                self.page = self.pages[action.tab]
        elif kind == "scroll":
            self.page.mouse.wheel(0, action.amount)
            self.resolver.reset()
        elif kind == "press_key" and not action.ref:
            self.page.keyboard.press(action.key)
            self.resolver.reset()
        else:
            handle = self.resolver.resolve(action.ref, action.observation_id)
            match kind:
                case "click":
                    handle.click()
                case "fill":
                    handle.fill(action.value)
                case "select_option":
                    handle.select_option(action.option)
                case "check":
                    handle.set_checked(action.checked)
                case "press_key":
                    handle.press(action.key)
                case "wait_for":
                    handle.wait_for_element_state("stable", timeout=action.timeout_ms)
            self.resolver.reset()

    def click(self, target, *, before_click=None, navigation=True):
        obs = self.observe()
        selection = STRATEGIES[self.mode].choose(self, target, obs)
        if before_click:
            before_click(obs, selection)
        try:
            started = time.monotonic()
            if "point" in selection:
                self.consume("click")
                x, y = selection["point"]
                handle = self.resolver.resolve(selection["ref"], selection["observation_id"])
                if not handle.evaluate(
                    "(e,p)=>{const hit=document.elementFromPoint(p.x,p.y);return hit===e||e.contains(hit)}",
                    {"x": x, "y": y},
                ):
                    raise BridgeError("LOCATOR_STALE", "Visual target changed before execution")
                if navigation:
                    with self.page.expect_navigation(wait_until="domcontentloaded"):
                        self.page.mouse.click(x, y)
                else:
                    self.page.mouse.click(x, y)
                self.resolver.reset()
            else:
                self.execute(
                    {"kind": "click", "ref": selection["ref"], "observation_id": selection["observation_id"]}
                )
            self.page.wait_for_load_state("domcontentloaded")
            self.check_page()
            return {**selection, "latency_ms": round((time.monotonic() - started) * 1000)}
        except PlaywrightError as exc:
            raise BridgeError("COMMIT_UNCERTAIN", "The browser did not confirm the action") from exc

    def field(self, locator, kind, value):
        obs = self.observe()
        ref = self.resolver.bind(locator)
        field = "option" if kind == "select_option" else "value"
        self.execute({"kind": kind, "ref": ref, "observation_id": obs.id, field: value})

    def prepare_intent(self, form, fields):
        # Server renders the protocol fields. This fixed code injects only the locally
        # persisted hash and version values, never model data or arbitrary JS.
        if form.count() != 1:
            raise BridgeError("TARGET_AMBIGUOUS", "Expected one authorized form")
        form.evaluate(
            """(e,fields) => {
            for (const [name,value] of Object.entries(fields)) {
              const input=e.elements.namedItem(name);
              if (!(input instanceof HTMLInputElement) || input.type!=='hidden') throw Error('protocol field missing');
              input.value=value ?? '';
            }
        }""",
            fields,
        )
