(() => {
  "use strict";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const LAYERS = ["entrypoint", "agent", "model", "warning"];
  const Y = { entrypoint: 82, agent: 232, model: 382, warning: 382 };
  const TITLES = {
    entrypoint: "Entrypoints", agent: "Agents", model: "Models",
    warning: "Warnings"
  };
  const COLORS = {
    invokes: "blue", spawns: "green",
    dispatches_to: "amber", reads_doc: "muted",
    subagent: "blue", skill: "green", command: "amber"
  };
  const BW = 154, BH = 52;
  let currentRoot = "", currentWorkflow = "roles";
  let currentHarness = "", graphs = {};
  const $ = (id) => document.getElementById(id);
  const pageState = $("page-state");
  const workflowSelect = $("workflow-select");
  const harnessSelect = $("harness-select");
  const topologySvg = $("topology-svg");
  const compareToggle = $("compare-toggle"), comparePanel = $("compare-panel");
  const compareList = $("compare-list");
  const homeToggle = $("home-toggle");
  let homeToggleTouched = false, homeQueryInitialized = false;
  // Match nav.js: the selected workspace is always sent to the API.
  function withRoot(url) {
    const params = new URLSearchParams({workflow: currentWorkflow});
    if (currentRoot) params.set("root", currentRoot);
    // Let the server choose the installed-copy default on the first request.
    if (homeQueryInitialized || homeToggleTouched) {
      params.set("home", homeToggle.checked ? "1" : "0");
    }
    return `${url}?${params}`;
  }
  function createSvg(name, attributes = {}) {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) =>
      element.setAttribute(key, String(value)));
    return element;
  }
  function svgText(value, attributes) {
    const text = createSvg("text", attributes); text.textContent = value; return text;
  }
  const fitLabel = (value) => value.slice(0, 24);
  // API values become text. Known kinds and edge types are the only classes.
  function parseGraphs(data) {
    const result = {};
    Object.entries(data.graphs).forEach(([name, raw]) => {
      const nodes = (raw && Array.isArray(raw.nodes) ? raw.nodes : [])
        .filter((node) => node && node.name != null)
        .map((node) => ({
          kind: LAYERS.includes(node.kind) ? node.kind : "agent",
          name: String(node.name), path: String(node.path || ""),
          output: Boolean(node.output),
          origin: node.origin === "installed" ? "installed" : "workspace"
        }));
      const edges = (raw && Array.isArray(raw.edges) ? raw.edges : [])
        .filter((edge) => edge && edge.src != null && edge.dst != null);
      if (nodes.length) result[name] = { nodes, edges };
    });
    return result;
  }
  const edgeType = (type) => Object.keys(COLORS).includes(type) ? type : "reads_doc";
  function edgePath(edge, positions) {
    const source = positions.get(edge.src), target = positions.get(edge.dst);
    const sameLayer = source.y === target.y;
    const side = target.x >= source.x ? 1 : -1;
    const startX = sameLayer ? source.x + side * BW / 2 : source.x;
    const endX = sameLayer ? target.x - side * BW / 2 : target.x;
    const startY = sameLayer ? source.y
      : source.y + (target.y > source.y ? BH / 2 : -BH / 2);
    const endY = sameLayer ? target.y
      : target.y + (source.y > target.y ? BH / 2 : -BH / 2);
    return sameLayer
      ? `M ${startX} ${startY} Q ${(startX + endX) / 2} ${
        source.y + 62} ${endX} ${endY}`
      : `M ${startX} ${startY} L ${endX} ${endY}`;
  }
  function drawNode(node, position) {
    const group = createSvg("g", { class: `topology-node kind-${node.kind}` });
    if (node.origin === "installed") group.classList.add("origin-installed");
    group.append(createSvg("rect", {
      x: position.x - BW / 2, y: position.y - BH / 2,
      width: BW, height: BH, rx: 4
    }), svgText(fitLabel(node.name), {
      class: "topology-node-label",
      x: position.x - BW / 2 + 10, y: position.y - 4
    }), svgText(`${node.kind} · ${node.origin}`, {
      class: "topology-node-meta",
      x: position.x - BW / 2 + 10, y: position.y + 14
    }));
    if (node.path) {
      group.classList.add("is-clickable");
      group.addEventListener("click", () => {
        window.location = `/skills.html?path=${encodeURIComponent(node.path)}`;
      });
    }
    topologySvg.append(group);
  }
  function renderGraph() {
    topologySvg.replaceChildren();
    const graph = graphs[currentHarness];
    if (!graph) {
      topologySvg.setAttribute("viewBox", "0 0 820 480");
      topologySvg.append(svgText("Choose a workspace to load its topology.", {
        class: "topology-node-meta", x: 24, y: 40
      }));
      $("topology-count").textContent = "—";
      $("topology-summary").textContent = "no topology loaded";
      return;
    }
    const layers = LAYERS.map((kind) =>
      graph.nodes.filter((node) => node.kind === kind));
    const largest = Math.max(1, ...layers.map((layer) => layer.length));
    const width = Math.max(820, largest * 180 + 40);
    const positions = new Map();
    layers.forEach((layer) => {
      const gap = width / (layer.length + 1);
      layer.forEach((node, index) => positions.set(node.name, {
        x: gap * (index + 1), y: Y[node.kind]
      }));
    });
    topologySvg.setAttribute("viewBox", `0 0 ${width} 480`);
    const edgeGroup = createSvg("g", { "aria-label": "Topology edges" });
    graph.edges.forEach((edge) => {
      if (!positions.has(edge.src) || !positions.has(edge.dst)) return;
      const type = edgeType(edge.type);
      edgeGroup.append(createSvg("path", {
        class: `topology-edge edge-${type}`,
        d: edgePath(edge, positions)
      }));
    });
    topologySvg.append(edgeGroup);
    LAYERS.forEach((kind) => topologySvg.append(svgText(TITLES[kind], {
      class: "topology-layer-label", x: 16, y: Y[kind] - 46
    })));
    graph.nodes.forEach((node) => drawNode(node, positions.get(node.name)));
    $("topology-count").textContent = `${graph.nodes.length} nodes`;
    $("topology-summary").textContent = currentHarness;
  }
  function renderHarnesses() {
    const names = Object.keys(graphs);
    harnessSelect.replaceChildren();
    if (!names.length) {
      harnessSelect.append(new Option("No harness graphs"));
      harnessSelect.disabled = true; currentHarness = "";
      return;
    }
    if (!names.includes(currentHarness)) currentHarness = names[0];
    names.forEach((name) => harnessSelect.append(new Option(name, name)));
    harnessSelect.value = currentHarness;
    harnessSelect.disabled = false;
  }
  const edgeKey = (edge) => `${edge.type}\u0000${edge.src}\u0000${edge.dst}`;
  function renderComparison() {
    compareList.replaceChildren();
    const names = Object.keys(graphs), variants = new Map();
    names.forEach((name) => graphs[name].edges.forEach((edge) => {
      const key = edgeKey(edge);
      const variant = variants.get(key) || { edge, harnesses: [] };
      if (!variant.harnesses.includes(name)) variant.harnesses.push(name);
      variants.set(key, variant);
    }));
    const differences = [...variants.values()].filter(
      (variant) => variant.harnesses.length < names.length);
    const outputVariants = new Map();
    names.forEach((name) => graphs[name].nodes
      .filter((node) => node.kind === "agent")
      .forEach((node) => {
        const variant = outputVariants.get(node.name) ||
          {name: node.name, values: new Map()};
        variant.values.set(name, node.output);
        outputVariants.set(node.name, variant);
      }));
    const outputDifferences = [...outputVariants.values()].filter((variant) =>
      new Set(variant.values.values()).size > 1);
    if (!differences.length && !outputDifferences.length) {
      const item = document.createElement("li");
      item.textContent = names.length ? "No wiring differences." : "Load workspace.";
      compareList.append(item);
      return;
    }
    differences.forEach(({ edge, harnesses }) => {
      const missing = names.filter((name) => !harnesses.includes(name));
      const item = document.createElement("li");
      item.textContent = `${edge.type}: ${edge.src} → ${edge.dst} · present in ${
        harnesses.join(", ")} · absent in ${missing.join(", ")}`;
      compareList.append(item);
    });
    outputDifferences.forEach(({name, values}) => {
      const item = document.createElement("li");
      const details = [...values.entries()]
        .map(([harness, output]) => `${harness}=${output ? "yes" : "no"}`)
        .join(", ");
      item.textContent = `output: ${name} · ${details}`;
      compareList.append(item);
    });
  }
  compareToggle.addEventListener("change", () => {
    comparePanel.hidden = !compareToggle.checked;
  });
  homeToggle.addEventListener("change", () => {
    homeToggleTouched = true;
    homeQueryInitialized = true;
    refresh();
  });
  harnessSelect.addEventListener("change", () => {
    currentHarness = harnessSelect.value;
    renderGraph();
  });
  workflowSelect.addEventListener("change", () => {
    currentWorkflow = workflowSelect.value;
    currentHarness = "";
    refresh();
  });
  async function refresh() {
    pageState.textContent = "scanning topology…";
    pageState.classList.remove("is-error");
    try {
      const response = await fetch(
        withRoot("/api/registry/topology"), { cache: "no-store" });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = data && typeof data === "object" ? data.error : data;
        throw new Error(detail || `${response.status} ${response.statusText}`);
      }
      if (!data || typeof data !== "object" || !data.graphs) {
        throw new Error("Topology response is missing graphs");
      }
      if (!homeToggleTouched && typeof data.include_home === "boolean") {
        homeToggle.checked = data.include_home;
        homeQueryInitialized = true;
      }
      graphs = parseGraphs(data);
      renderHarnesses(); renderGraph(); renderComparison();
      pageState.textContent = `${Object.keys(graphs).length} harnesses loaded`;
    } catch (error) {
      graphs = {}; currentHarness = "";
      renderHarnesses(); renderGraph(); renderComparison();
      pageState.textContent = error.message;
      pageState.classList.add("is-error");
    }
  }
  window.addEventListener("trio:workspace", (event) => {
    currentRoot = event.detail && event.detail.path ? event.detail.path : "";
    workflowSelect.disabled = !currentRoot;
    refresh();
  });
})();
