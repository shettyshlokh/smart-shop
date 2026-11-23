/* Location helpers shared by all pages (OpenStreetMap - free, no API key) */
const INDIA = [20.59, 78.96];

function baseMap(id, center, zoom) {
  if (!window.L) {                       // map library could not load (no internet)
    document.getElementById(id).style.display = 'none';
    return null;
  }
  const map = L.map(id, { scrollWheelZoom: false }).setView(center, zoom);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);
  return map;
}

/* Ask the browser for the user's GPS location */
function getMyLocation() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject('Your browser does not support location.');
    navigator.geolocation.getCurrentPosition(
      p => resolve({ lat: p.coords.latitude, lng: p.coords.longitude }),
      e => reject(e.code === 1 ? 'Location permission was denied. Allow it in your browser, or search your area instead.'
                               : 'Could not get your location. Try searching your area instead.'),
      { enableHighAccuracy: true, timeout: 15000 });
  });
}

/* Turn an address / area name into coordinates */
async function geocode(query) {
  const url = 'https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=in&q='
              + encodeURIComponent(query);
  const res = await fetch(url, { headers: { 'Accept-Language': 'en' } });
  const data = await res.json();
  if (!data.length) throw 'Place not found. Try adding the city name.';
  return { lat: +data[0].lat, lng: +data[0].lon, label: data[0].display_name.split(',').slice(0, 2).join(',') };
}

const shopIcon = window.L && L.divIcon({ className: 'pin', html: '🏪', iconSize: [30, 30], iconAnchor: [15, 28] });
const meIcon   = window.L && L.divIcon({ className: 'pin', html: '📍', iconSize: [30, 30], iconAnchor: [15, 28] });
