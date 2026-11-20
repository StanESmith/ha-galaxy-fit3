// The Galaxy Fit3 view strategy: what it builds from the integration's data (node --test tests_js).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../custom_components/galaxy_fit3/frontend/galaxy-fit3.js", import.meta.url), "utf8");
// import the plain .js file without a package.json that marks it a module
const strategy = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));

const ENTITIES = {
  battery: "sensor.galaxy_fit3_battery",
  heart_rate: "sensor.galaxy_fit3_heart_rate",
  steps: "sensor.galaxy_fit3_steps",
  watch_face: "select.galaxy_fit3_watch_face",
  store_face: "select.galaxy_fit3_store_face",
  install: "sensor.galaxy_fit3_face_install",
  install_button: "button.galaxy_fit3_install_watch_face",
};
const FACES = [
  { id: 106, name: "Fitness pro 3", preview: "/local/galaxy_fit3/faces/face_106.png" },
  { id: 16, name: "Progress gauge 1", preview: "https://img.example/16.png" },
  { id: 31, name: "Casual Minimalist", preview: null },
];
const DATA = { entry_id: "abc", title: "Galaxy Fit3", entities: ENTITIES, faces: FACES };

const cardsOf = (view, section = 0) => view.sections[section].cards;
const faceCards = (view) => cardsOf(view).filter((c) => c.type === "picture-entity");

test("the faces page has one switch card per face with a visibility condition by face id", () => {
  const cards = faceCards(strategy.buildView({}, DATA));
  assert.equal(cards.length, 3);
  const card = cards.find((c) => c.name === "Progress gauge 1");
  assert.equal(card.image, "https://img.example/16.png");
  assert.equal(card.tap_action.perform_action, "galaxy_fit3.set_watch_face");
  assert.deepEqual(card.tap_action.data, { face_id: 16 });
  assert.equal(card.tap_action.confirmation.text, "Switch the watch to Progress gauge 1?");
  assert.equal(
    card.visibility[0].value_template,
    "{{ 16 in (state_attr('select.galaxy_fit3_watch_face', 'face_ids') or []) and not " +
      "(state_attr('select.galaxy_fit3_watch_face', 'face_id') == 16) }}",
  );
  assert.deepEqual(card.grid_options, { columns: 3, rows: "auto" });
});

test("a face without a preview gets the placeholder picture", () => {
  const card = faceCards(strategy.buildView({}, DATA)).find((c) => c.name === "Casual Minimalist");
  assert.match(card.image, /^data:image\/svg\+xml/);
});

test("the store page installs a face in the background and shows the faces that are not on the watch", () => {
  const view = strategy.buildView({ page: "store" }, DATA);
  assert.equal(view.max_columns, 4);
  const card = faceCards(view).find((c) => c.name === "Fitness pro 3");
  assert.equal(card.tap_action.perform_action, "galaxy_fit3.install_watch_face");
  assert.deepEqual(card.tap_action.data, { face_id: 106, wait: false });
  assert.match(card.tap_action.confirmation.text, /^Install Fitness pro 3 on the watch\?/);
  assert.equal(card.visibility[0].value_template, "{{ not (106 in (state_attr('select.galaxy_fit3_watch_face', 'face_ids') or [])) }}");
  assert.equal(cardsOf(view).find((c) => c.type === "tile").entity, ENTITIES.install);
});

test("the faces page links to the store view, by default and when its path is configured", () => {
  const link = (config) => cardsOf(strategy.buildView(config, DATA), 1).find((c) => c.type === "shortcut");
  assert.equal(link({}).tap_action.navigation_path, "galaxy-fit3-store");
  assert.equal(link({ store_path: "/lovelace/my-store" }).tap_action.navigation_path, "/lovelace/my-store");
});

test("the store link is built from the dashboard that shows the view", () => {
  globalThis.location = { pathname: "/dashboard-watch/galaxy-fit3" };
  try {
    const link = cardsOf(strategy.buildView({}, DATA), 1).find((c) => c.type === "shortcut");
    assert.equal(link.tap_action.navigation_path, "/dashboard-watch/galaxy-fit3-store");
    assert.equal(strategy.storePath({ store_path: "/x/y" }), "/x/y");
  } finally {
    delete globalThis.location;
  }
});

test("the faces page has the battery badge, the current face, and the health section unless turned off", () => {
  const view = strategy.buildView({}, DATA);
  assert.deepEqual(cardsOf(view)[0].badges, [{ type: "entity", entity: ENTITIES.battery, show_state: true }]);
  assert.match(cardsOf(view)[1].content, /state_attr\('select\.galaxy_fit3_watch_face', 'entity_picture'\)/);
  assert.equal(view.sections.length, 3);
  assert.equal(strategy.buildView({ show_health: false }, DATA).sections.length, 2);
  const stats = cardsOf(view, 2).filter((c) => c.type === "statistics-graph");
  assert.deepEqual(stats[0].entities, [{ entity: "galaxy_fit3:heart_rate", name: "Heart rate" }]);
  assert.equal(stats[1].entities[0].entity, ENTITIES.steps);
});

test("entities that are not registered are left out instead of breaking the page", () => {
  const bare = { ...DATA, entities: { ...ENTITIES, battery: null, heart_rate: null, steps: null, install: null } };
  const view = strategy.buildView({}, bare);
  assert.equal(cardsOf(view)[0].badges, undefined);
  assert.equal(cardsOf(view, 1).some((c) => c.type === "tile"), false);
  const health = cardsOf(view, 2);
  assert.equal(health.some((c) => c.type === "tile"), false);
  assert.equal(health.filter((c) => c.type === "statistics-graph").length, 1);
});

test("with no faces both pages say so", () => {
  const empty = { ...DATA, faces: [] };
  for (const page of ["faces", "store"]) {
    const cards = cardsOf(strategy.buildView({ page }, empty));
    assert.equal(cards.some((c) => c.type === "picture-entity"), false);
    assert.equal(cards.some((c) => c.type === "markdown" && /No faces to show yet/.test(c.content)), true);
  }
});

test("generate asks the integration for the faces, naming the entry when configured", async () => {
  const calls = [];
  const hass = { callWS: async (msg) => (calls.push(msg), DATA) };
  const view = await strategy.GalaxyFit3ViewStrategy.generate({ entry_id: "abc", page: "store" }, hass);
  assert.deepEqual(calls, [{ type: "galaxy_fit3/faces", entry_id: "abc" }]);
  assert.equal(view.max_columns, 4);
  await strategy.GalaxyFit3ViewStrategy.generate({}, hass);
  assert.deepEqual(calls[1], { type: "galaxy_fit3/faces" });
});

test("when the integration is not there the view says why", async () => {
  const hass = { callWS: async () => { throw { message: "no Galaxy Fit3 is set up" }; } };
  const view = await strategy.GalaxyFit3ViewStrategy.generate({}, hass);
  assert.match(cardsOf(view)[1].content, /could not be built[\s\S]*no Galaxy Fit3 is set up/);
});
