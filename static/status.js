// The status page: poll GET /status, patch the DOM in place.
//
// Replaces a `<meta http-equiv="refresh">` that reloaded the whole document
// every two seconds. Three things that bought:
//
//   - scroll position survives, so reading a long warning list is possible;
//   - one access-log line per poll became none (see observability/logs.py);
//   - a failed poll shows as "disconnected" rather than a browser error page,
//     which matters because the most interesting moment for this page is
//     exactly when perform has stopped answering.
//
// Vanilla, no build step, no framework — the same posture as the surface page
// and as wander's server-rendered UI. Rendering lives only here rather than
// being duplicated in Jinja and left to drift.

(function () {
  "use strict";

  var POLL_MS = 2000;
  var el = function (id) { return document.getElementById(id); };
  var timer = null;
  var failures = 0;

  // --- tiny DOM helpers ---------------------------------------------------

  function node(tag, className, text) {
    var n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function span(className, text) { return node("span", className, text); }

  function db(value) {
    // Always signed: "-6.0 dB" and "+0.0 dB" read as levels, "0" reads as
    // "unset" and sends people looking for a bug.
    var n = Number(value || 0);
    return (n >= 0 ? "+" : "") + n.toFixed(1) + " dB";
  }

  function secs(value) {
    return value === null || value === undefined ? null : Number(value).toFixed(1) + "s";
  }

  // --- rendering ----------------------------------------------------------

  function renderHeader(s) {
    var device = el("device");
    device.textContent = "device " + (s.device.running ? "running" : "down");
    device.className = "chip " + (s.device.running ? "ok" : "bad");

    var xruns = el("xruns");
    xruns.hidden = !s.device.xruns;
    xruns.textContent = s.device.xruns + " xrun(s)";

    // Frames rendered is the honest measure of "is the audio clock actually
    // turning", which a health check cannot tell you.
    el("rendered").textContent = (s.device.frames_rendered / 48000).toFixed(1) + "s rendered";
  }

  function renderVerdict(s) {
    var v = el("verdict");
    // The one line someone actually came here to read.
    v.textContent = s.quiet_because || "playing";
    v.className = s.quiet_because ? "verdict" : "verdict playing";
  }

  function renderItem(item, extraClass) {
    var row = node("div", "item " + (extraClass || ""));
    row.appendChild(span("label", item.label || item.id));
    row.appendChild(span("asset", item.asset));

    var bits = [item.duration.kind];
    var elapsed = secs(item.elapsed_s);
    if (elapsed) bits.push(elapsed + (secs(item.total_s) ? " / " + secs(item.total_s) : ""));
    if (item.cycles) bits.push("cycle " + item.cycles);
    if (item.treatment && item.treatment !== "clean") bits.push(item.treatment);
    row.appendChild(span("dim", bits.join(" · ")));
    return row;
  }

  function renderBus(name, b) {
    var article = node("article", "bus state-" + b.state);

    var h2 = node("h2", null, name);
    h2.appendChild(span("state", b.state));
    h2.appendChild(span("sink", "→ " + b.sink));
    article.appendChild(h2);

    var levels = node("p", "levels", db(b.gain_db));
    if (b.duck_db) {
      levels.appendChild(
        span("ducked", " ducked " + db(b.duck_db) + " → " + db(b.effective_db))
      );
    }
    if (b.resting_open_ended) levels.appendChild(span("dim", " rests open-ended"));
    b.ducks.forEach(function (d) {
      levels.appendChild(span("dim", " ducks " + d.target + " " + db(d.depth_db)));
    });
    article.appendChild(levels);

    article.appendChild(
      b.current ? renderItem(b.current, "current") : node("div", "item empty", "nothing playing")
    );

    if (b.pending.length) {
      var ol = node("ol", "pending");
      b.pending.forEach(function (p) {
        var li = node("li");
        li.appendChild(span("label", p.label || p.id));
        li.appendChild(span("asset", p.asset));
        ol.appendChild(li);
      });
      article.appendChild(ol);
    }

    if (b.recent.length) {
      var ul = node("ul", "recent");
      b.recent.forEach(function (r) {
        var li = node("li", "s-" + r.state);
        li.appendChild(span("badge", r.state));
        li.appendChild(span("label", r.label || r.id));
        // The note is why an item was dropped — the whole reason terminal
        // items are kept around instead of popped.
        if (r.note) li.appendChild(span("note", r.note));
        ul.appendChild(li);
      });
      article.appendChild(ul);
    }

    return article;
  }

  function renderBuses(s) {
    var container = el("buses");
    container.replaceChildren.apply(
      container,
      Object.keys(s.buses).map(function (name) { return renderBus(name, s.buses[name]); })
    );
  }

  function renderSinks(s) {
    var section = el("sinks");
    var table = section.querySelector("table");
    var names = Object.keys(s.sinks);
    section.hidden = !names.length;

    table.replaceChildren.apply(
      table,
      names.map(function (name) {
        var tr = node("tr");
        tr.appendChild(node("th", null, name));
        // Sinks report free-form dicts, so render scalars as `k=v` and
        // summarize anything nested rather than pasting raw JSON into the page.
        var status = s.sinks[name];
        var detail = Object.keys(status)
          .map(function (k) {
            var v = status[k];
            if (v !== null && typeof v === "object") {
              var count = Object.keys(v).length;
              return count ? k + "=" + Object.keys(v).join(",") : k + "=none";
            }
            return k + "=" + v;
          })
          .join(" · ");
        tr.appendChild(node("td", null, detail));
        return tr;
      })
    );
  }

  function renderWarnings(s) {
    var section = el("warnings");
    var ul = section.querySelector("ul");
    var rows = [];

    s.warnings.forEach(function (w) {
      var li = node("li");
      li.appendChild(span("badge", w.kind));
      li.appendChild(span("dim", w.bus + " · " + w.age_s + "s ago"));
      li.appendChild(span(null, w.detail));
      rows.push(li);
    });
    (s.library_warnings || []).forEach(function (w) {
      var li = node("li");
      li.appendChild(span("badge", "library"));
      li.appendChild(span(null, w));
      rows.push(li);
    });

    section.hidden = !rows.length;
    ul.replaceChildren.apply(ul, rows);
  }

  function render(s) {
    renderHeader(s);
    renderVerdict(s);
    renderBuses(s);
    renderSinks(s);
    renderWarnings(s);
  }

  // --- polling ------------------------------------------------------------

  function connected(ok, detail) {
    var chip = el("conn");
    chip.textContent = ok ? "live" : detail || "disconnected";
    chip.className = "chip " + (ok ? "ok" : "bad");
  }

  function poll() {
    // same-origin credentials so a session cookie would work if one is ever
    // added; a bearer token cannot ride along on a page fetch, hence the
    // explicit 403 message below.
    fetch("/status", {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) {
        if (r.status === 403) {
          var denied = new Error("forbidden");
          denied.forbidden = true;
          throw denied;
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (s) {
        failures = 0;
        connected(true);
        render(s);
      })
      .catch(function (e) {
        // The most interesting moment for this page is when perform has
        // stopped answering, so say so and keep trying rather than blanking.
        if (e && e.forbidden) {
          // Perform is up and refusing us. Saying "not responding" here would
          // send someone to check whether the process is alive.
          stop();
          connected(false, "PERFORM_TOKEN is set — /status needs a bearer token");
          el("verdict").textContent =
            "This page reads /status, which is protected while PERFORM_TOKEN is set. " +
            "The browser dashboard is a no-token (loopback) affordance; use " +
            "curl -H 'Authorization: Bearer …' localhost:8770/status instead.";
          return;
        }
        failures += 1;
        connected(false, failures > 2 ? "perform is not responding" : "reconnecting…");
      });
  }

  function start() {
    if (timer !== null) return;
    poll();
    timer = setInterval(poll, POLL_MS);
  }

  function stop() {
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  // Deliberately no visibilitychange pause.
  //
  // Pausing a backgrounded tab looks like free politeness and is not. It froze
  // the last render on screen with the chip still reading "live", so the page
  // showed stale numbers as current — the exact failure this dashboard exists
  // to catch. Worse, it only half-applied: `visibilitychange` fires on a
  // transition, so a page *loaded* while hidden never paused at all, and the
  // behaviour depended on whether the tab happened to be visible at load.
  //
  // The saving was one small fetch to loopback every two seconds. Not worth
  // two failure modes, so the page simply polls while it exists.

  start();
})();
