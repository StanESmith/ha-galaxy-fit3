/*
 * Galaxy Fit3 dashboard strategy.
 *
 * A view strategy: Home Assistant calls generate() when the dashboard loads and shows what it returns.
 *
 *   views:
 *     - title: Galaxy Fit3
 *       path: galaxy-fit3
 *       icon: mdi:watch-variant
 *       strategy:
 *         type: custom:galaxy-fit3
 *     - title: Face store
 *       path: galaxy-fit3-store
 *       icon: mdi:storefront-outline
 *       subview: true
 *       back_path: galaxy-fit3
 *       strategy:
 *         type: custom:galaxy-fit3
 *         page: store
 *
 * Options: page (faces | store), entry_id (when there is more than one watch), store_path (the store
 * view's path, default galaxy-fit3-store, looked up on the dashboard being shown) and show_health
 * (default true). The store view's back_path must be written out in the view itself.
 *
 * The integration loads this file itself; the faces and entity ids come from its galaxy_fit3/faces
 * WebSocket command. Cards show or hide with visibility conditions, so a face that is installed moves
 * from the store page to the grid without reloading.
 */

const WS_FACES = "galaxy_fit3/faces";
const HEART_RATE_STATISTIC = "galaxy_fit3:heart_rate";
const PLACEHOLDER =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 402"><rect width="256" height="402" rx="48" ' +
      'fill="#37474f"/><circle cx="128" cy="201" r="72" fill="none" stroke="#90a4ae" stroke-width="14"/>' +
      '<path d="M128 150v51l34 22" fill="none" stroke="#90a4ae" stroke-width="14" stroke-linecap="round"/></svg>',
  );

/** The store view's path, absolute so the link works from whichever dashboard holds the view. */
export function storePath(config) {
  const path = config.store_path || "galaxy-fit3-store";
  if (path.startsWith("/")) return path;
  const dashboard = typeof location !== "undefined" ? location.pathname.split("/")[1] : "";
  return dashboard ? `/${dashboard}/${path}` : path;
}

const onWatch = (select, id) => `${id} in (state_attr('${select}', 'face_ids') or [])`;
const isCurrent = (select, id) => `state_attr('${select}', 'face_id') == ${id}`;

/** One picture card for a face: on the grid (tap switches) or in the store (tap installs). */
export function faceCard(page, face, entities) {
  const select = entities.watch_face;
  const store = page === "store";
  return {
    type: "picture-entity",
    entity: select,
    image: face.preview || PLACEHOLDER,
    name: face.name,
    show_name: true,
    show_state: false,
    grid_options: { columns: store ? 4 : 3, rows: "auto" },
    tap_action: store
      ? {
          action: "perform-action",
          perform_action: "galaxy_fit3.install_watch_face",
          data: { face_id: face.id, wait: false },
          confirmation: {
            text: `Install ${face.name} on the watch? It takes about a minute; keep the watch near Home Assistant.`,
          },
        }
      : {
          action: "perform-action",
          perform_action: "galaxy_fit3.set_watch_face",
          data: { face_id: face.id },
          confirmation: { text: `Switch the watch to ${face.name}?` },
        },
    hold_action: { action: "none" },
    visibility: [
      {
        condition: "template",
        value_template: store
          ? `{{ not (${onWatch(select, face.id)}) }}`
          : `{{ ${onWatch(select, face.id)} and not (${isCurrent(select, face.id)}) }}`,
      },
    ],
  };
}

function installTile(entities) {
  return entities.install
    ? {
        type: "tile",
        entity: entities.install,
        name: "Install",
        vertical: false,
        state_content: ["state", "face", "progress"],
        grid_options: { columns: "full", rows: 1 },
      }
    : null;
}

function currentFaceCard(entities) {
  const s = entities.watch_face;
  return {
    type: "markdown",
    grid_options: { columns: 12, rows: "auto" },
    content:
      `{% set p = state_attr('${s}', 'entity_picture') %}{% if p %}<img src="{{ p }}" width="96" align="left" ` +
      `style="margin-right:16px">{% endif %}\n<small>Showing now</small>\n### {{ states('${s}') if states('${s}') ` +
      `not in ['unknown', 'unavailable'] else 'Waiting for the watch' }}\n<small>Tap a face below to switch. ` +
      `The watch changes on its next connection, a few seconds when it is near Home Assistant.</small>`,
  };
}

function healthSection(entities) {
  const trend = [{ type: "trend-graph", hours_to_show: 24 }];
  const cards = [{ type: "heading", heading: "Health", icon: "mdi:heart-pulse" }];
  if (entities.heart_rate) {
    cards.push({
      type: "tile", entity: entities.heart_rate, name: "Heart rate", vertical: false, features: trend,
      grid_options: { columns: 6, rows: 2 },
    });
  }
  if (entities.steps) {
    cards.push({
      type: "tile", entity: entities.steps, name: "Steps today", vertical: false, features: trend,
      grid_options: { columns: 6, rows: 2 },
    });
  }
  cards.push({
    type: "statistics-graph", title: "Heart rate, last 7 days (hourly)",
    entities: [{ entity: HEART_RATE_STATISTIC, name: "Heart rate" }],
    stat_types: ["mean", "min", "max"], period: "hour", days_to_show: 7, chart_type: "line",
    grid_options: { columns: "full", rows: 5 },
  });
  if (entities.steps) {
    cards.push({
      type: "statistics-graph", title: "Steps per day", entities: [{ entity: entities.steps, name: "Steps" }],
      stat_types: ["max"], period: "day", days_to_show: 14, chart_type: "bar",
      grid_options: { columns: "full", rows: 4 },
    });
  }
  if (entities.heart_rate) {
    const hr = entities.heart_rate;
    cards.push({
      type: "markdown", text_only: true, grid_options: { columns: "full", rows: "auto" },
      content:
        `<small>Last reading from the watch: {{ state_attr('${hr}', 'last_seen') | as_datetime | as_local | ` +
        `relative_time if state_attr('${hr}', 'last_seen') else 'not yet' }} ago. ` +
        `{{ state_attr('${hr}', 'stored_records') or 0 }} heart-rate readings stored.</small>`,
    });
  }
  return { type: "grid", cards };
}

const NO_FACES = {
  type: "markdown",
  grid_options: { columns: "full", rows: "auto" },
  content: "No faces to show yet. The face list comes from Samsung's store when Home Assistant can reach it.",
};

/** The page of faces that are on the watch. */
function facesView(config, data) {
  const { entities, faces } = data;
  const heading = { type: "heading", heading: "Watch face", icon: "mdi:watch-variant" };
  if (entities.battery) heading.badges = [{ type: "entity", entity: entities.battery, show_state: true }];
  const grid = [
    heading,
    currentFaceCard(entities),
    { type: "heading", heading: "Tap to switch", heading_style: "subtitle" },
    ...(faces.length ? faces.map((face) => faceCard("faces", face, entities)) : [NO_FACES]),
  ];
  const add = [
    { type: "heading", heading: "Add a face", icon: "mdi:storefront-outline" },
    {
      type: "shortcut", label: "Browse the face store", icon: "mdi:storefront-outline",
      tap_action: { action: "navigate", navigation_path: storePath(config) },
      grid_options: { columns: 12, rows: 1 },
    },
  ];
  const tile = installTile(entities);
  if (tile) add.push({ ...tile, grid_options: { columns: 12, rows: 1 } });
  const sections = [
    { type: "grid", cards: grid },
    { type: "grid", cards: add },
  ];
  if (config.show_health !== false) sections.push(healthSection(entities));
  return { type: "sections", max_columns: 3, sections };
}

/** The page of faces that are not on the watch yet, with a tap to install. */
function storeView(config, data) {
  const { entities, faces } = data;
  const cards = [
    { type: "heading", heading: "Galaxy Fit3 face store", icon: "mdi:storefront-outline" },
    {
      type: "markdown", text_only: true, grid_options: { columns: "full", rows: "auto" },
      content:
        "<small>Every free face in Samsung's store that is not on the watch yet. Tap one to install it: it takes " +
        "about a minute, keep the watch near Home Assistant. When it is done it moves to the Watch face page.</small>",
    },
  ];
  const tile = installTile(entities);
  if (tile) cards.push(tile);
  cards.push(...(faces.length ? faces.map((face) => faceCard("store", face, entities)) : [NO_FACES]));
  return { type: "sections", max_columns: 4, sections: [{ type: "grid", column_span: 4, cards }] };
}

/** The view for a strategy config and the data from galaxy_fit3/faces. Pure, so it can be tested. */
export function buildView(config, data) {
  return config.page === "store" ? storeView(config, data) : facesView(config, data);
}

/** A view that says what went wrong instead of an empty page. */
export function errorView(message) {
  return {
    type: "sections",
    sections: [
      {
        type: "grid",
        cards: [
          { type: "heading", heading: "Galaxy Fit3", icon: "mdi:watch-variant" },
          { type: "markdown", content: `The Galaxy Fit3 face view could not be built.\n\n**${message}**` },
        ],
      },
    ],
  };
}

const Base = typeof HTMLElement !== "undefined" ? HTMLElement : class {};

class GalaxyFit3ViewStrategy extends Base {
  static async generate(config, hass) {
    let data;
    try {
      data = await hass.callWS({ type: WS_FACES, ...(config.entry_id ? { entry_id: config.entry_id } : {}) });
    } catch (err) {
      return errorView(err?.message || "The Galaxy Fit3 integration is not set up or not loaded.");
    }
    return buildView(config, data);
  }
}

if (typeof customElements !== "undefined" && !customElements.get("ll-strategy-view-galaxy-fit3")) {
  customElements.define("ll-strategy-view-galaxy-fit3", GalaxyFit3ViewStrategy);
}

export { GalaxyFit3ViewStrategy };
