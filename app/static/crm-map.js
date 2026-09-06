/*
 * The map view's only script.
 *
 * Markers are read from a JSON data island rather than from generated
 * JavaScript, so a record whose name contains a quote is data here and not a
 * hole. Nothing is written back: the map is a way of looking at records, and
 * clicking one navigates to it like any other link.
 */
(function () {
  "use strict";

  const host = document.getElementById("map");
  const data = document.getElementById("map-markers");
  if (!host || !data || typeof L === "undefined") return;

  let markers = [];
  try {
    markers = JSON.parse(data.textContent) || [];
  } catch (err) {
    return; // The accessible list below the map still has every record.
  }

  const colours = {
    green: "#1a7f4b",
    red: "#d4342c",
    amber: "#a86400",
    blue: "#1f6f8b",
    accent: "#2f5cff",
  };

  const map = L.map(host, { scrollWheelZoom: false });
  L.tileLayer(host.dataset.tileUrl, {
    attribution: host.dataset.attribution || "",
    maxZoom: 19,
  }).addTo(map);

  const points = [];
  markers.forEach(function (marker) {
    const colour = colours[marker.colour] || colours.accent;
    const pin = L.circleMarker([marker.lat, marker.lon], {
      radius: 7,
      weight: 2,
      color: colour,
      fillColor: colour,
      fillOpacity: 0.65,
    }).addTo(map);

    // Built as elements, not as a string: the popup carries record text.
    const popup = document.createElement("div");
    const link = document.createElement("a");
    link.href = marker.url;
    link.textContent = marker.title;
    popup.appendChild(link);
    if (marker.subtitle) {
      const sub = document.createElement("div");
      sub.className = "muted";
      sub.textContent = marker.subtitle;
      popup.appendChild(sub);
    }
    pin.bindPopup(popup);
    points.push([marker.lat, marker.lon]);
  });

  if (points.length) {
    map.fitBounds(points, {
      padding: [30, 30],
      maxZoom: Number(host.dataset.zoom) || 10,
    });
  } else {
    map.setView([20, 0], 2);
  }
})();
