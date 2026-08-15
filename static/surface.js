// The visual surface client.
//
// Its whole job is to render what the server says and to report back honestly
// when something ends. That second half is what SPEC §4 means by "reports
// completion — the page can report that a video ended or an animation hit its
// loop count, which is what keeps visuals sequencing exactly like audio." If
// this file lies about completions, the visual bus drifts away from the audio
// bus and nothing upstream can tell.
//
// EventSource, not a socket: it reconnects on its own. A closed laptop lid
// should not need recovery code.

(function () {
  "use strict";

  const stage = document.getElementById("stage");
  const idle = document.getElementById("idle");
  const statusEl = document.getElementById("status");

  // One layer per bus. Buses are fixed at startup, so this only ever grows to
  // the handful the config declares.
  const layers = new Map();

  function layerFor(bus) {
    let layer = layers.get(bus);
    if (!layer) {
      layer = document.createElement("div");
      layer.className = "layer";
      layer.dataset.bus = bus;
      stage.appendChild(layer);
      layers.set(bus, layer);
    }
    return layer;
  }

  function report(itemId, natural, detail) {
    // Fire-and-forget. If this POST fails the bus stalls, which /status will
    // say out loud — far better than the page silently pretending it played.
    fetch("/surface/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: itemId, natural: natural, detail: detail || "" }),
    }).catch(function (e) {
      note("could not report completion: " + e.message);
    });
  }

  function note(message) {
    if (!message) {
      statusEl.hidden = true;
      return;
    }
    statusEl.textContent = message;
    statusEl.hidden = false;
  }

  function show(event) {
    idle.hidden = true;
    const layer = layerFor(event.bus);
    const duration = event.duration || {};

    const el =
      event.media === "video"
        ? document.createElement("video")
        : document.createElement("img");

    el.src = event.url;

    if (event.media === "video") {
      // Not negotiable, and not a workaround. Audio and video stay separate;
      // a video's own track is never heard. That this also makes autoplay
      // unconditional is a happy side effect, not the reason.
      el.muted = true;
      el.playsInline = true;
      el.autoplay = true;

      if (duration.kind === "repeat" && duration.repeat > 1) {
        // Loop counting lives here because only the page knows when a cycle
        // actually ended. Reporting after exactly N is what "one cycle times
        // N" means; looping forever and guessing at the clock is what it does
        // not mean.
        let cycles = 0;
        el.loop = false;
        el.addEventListener("ended", function () {
          cycles += 1;
          if (cycles >= duration.repeat) {
            report(event.item_id, true);
          } else {
            el.currentTime = 0;
            el.play();
          }
        });
      } else if (duration.kind === "until_replaced") {
        // Open-ended: hold it. The server replaces or stops it; we never
        // report completion, and the bus resting here is either its steady
        // state or a hang, which /status decides.
        el.loop = true;
      } else {
        el.loop = false;
        el.addEventListener("ended", function () {
          report(event.item_id, true);
        });
      }

      el.addEventListener("error", function () {
        // Unblock the bus, but say it did not play. A decode failure that
        // reported success would leave the scene looking fine and sounding
        // wrong.
        report(event.item_id, false, "video failed to load or decode");
      });
    } else {
      el.addEventListener("error", function () {
        report(event.item_id, false, "image failed to load");
      });

      if (duration.kind === "declared" && duration.seconds) {
        // A still has no intrinsic end, so the page holds it for the stated
        // interval and then says so. The server arms a backstop timer too and
        // warns if it fires first — that combination is what catches a page
        // that stopped reporting.
        setTimeout(function () {
          report(event.item_id, true);
        }, duration.seconds * 1000);
      } else if (duration.kind !== "until_replaced") {
        // An image with an intrinsic duration is a contradiction; treat it as
        // shown and move on rather than hanging the bus forever.
        report(event.item_id, true);
      }
    }

    const transition = event.transition === "cut" ? "cut" : "";
    layer.className = "layer " + transition;
    layer.replaceChildren(el);
    // Next frame, so the opacity transition actually runs.
    requestAnimationFrame(function () {
      layer.classList.add("visible");
    });

    if (event.audio_stripped) {
      // Visible on the surface as well as in /status. SPEC §4 warns this case
      // "will fail silently rather than error", and one of the two places
      // someone will be looking is the screen itself.
      note("this video carried an audio track — playing muted");
      setTimeout(function () { note(""); }, 4000);
    }
  }

  function clear(event) {
    const layer = layers.get(event.bus);
    if (layer) {
      layer.classList.remove("visible");
      setTimeout(function () { layer.replaceChildren(); }, 400);
    }
    if (Array.from(layers.values()).every(function (l) { return !l.classList.contains("visible"); })) {
      idle.hidden = false;
    }
  }

  function connect() {
    const source = new EventSource("/surface/events");

    source.addEventListener("show", function (e) { show(JSON.parse(e.data)); });
    source.addEventListener("clear", function (e) { clear(JSON.parse(e.data)); });

    source.addEventListener("open", function () { note(""); });
    source.addEventListener("error", function () {
      // EventSource retries on its own; this only tells the human. A surface
      // that quietly stopped receiving events looks exactly like one with
      // nothing to show, which is the confusion worth spending a badge on.
      note("disconnected from perform — retrying");
    });
  }

  connect();
})();
