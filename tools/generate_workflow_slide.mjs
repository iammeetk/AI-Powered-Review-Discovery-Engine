import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

async function loadArtifactTool() {
  if (process.env.ARTIFACT_TOOL_MODULE) {
    return import(pathToFileURL(process.env.ARTIFACT_TOOL_MODULE).href);
  }
  return import("@oai/artifact-tool");
}

async function writeBlob(filePath, blob) {
  await fs.writeFile(filePath, new Uint8Array(await blob.arrayBuffer()));
}

function pct(value) {
  return `${Math.round((value || 0) * 100)}%`;
}

function addText(slide, text, position, style = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    position,
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = text;
  shape.text.style = style;
  return shape;
}

function addCard(slide, title, body, position, accent) {
  slide.shapes.add({
    geometry: "roundRect",
    position,
    fill: "white",
    line: { style: "solid", fill: "slate-200", width: 1 },
    borderRadius: "rounded-lg",
    shadow: "shadow-sm",
  });
  slide.shapes.add({
    geometry: "rect",
    position: { left: position.left, top: position.top, width: 8, height: position.height },
    fill: accent,
    line: { style: "solid", fill: accent, width: 0 },
  });
  addText(slide, title, {
    left: position.left + 24,
    top: position.top + 22,
    width: position.width - 44,
    height: 34,
  }, { fontSize: 22, bold: true, color: "slate-950", typeface: "Aptos Display" });
  addText(slide, body, {
    left: position.left + 24,
    top: position.top + 66,
    width: position.width - 44,
    height: position.height - 86,
  }, { fontSize: 15, color: "slate-600", typeface: "Aptos" });
}

async function main() {
  const [summaryPath, outputDir] = process.argv.slice(2);
  if (!summaryPath || !outputDir) {
    throw new Error("Usage: node generate_workflow_slide.mjs <summary.json> <output-dir>");
  }

  const { Presentation, PresentationFile } = await loadArtifactTool();
  const summary = JSON.parse(await fs.readFile(summaryPath, "utf8"));
  await fs.mkdir(outputDir, { recursive: true });

  const topTheme = summary.theme_distribution?.[0] || { label: "Discovery friction", share: 0 };
  const topSegment = summary.user_segments?.[0] || { label: "Spotify listeners", share: 0 };
  const topIntent = summary.intent_distribution?.[0] || { label: "Find new music", share: 0 };
  const sources = (summary.data_sources_used || []).map((item) => item.source).join(", ");

  const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
  const slide = presentation.slides.add();
  slide.background.fill = "slate-50";

  addText(slide, "AI Review Discovery Engine", { left: 64, top: 42, width: 620, height: 44 }, {
    fontSize: 34,
    bold: true,
    color: "slate-950",
    typeface: "Aptos Display",
  });
  addText(slide, "Reviews -> AI Analysis -> Insights", { left: 64, top: 92, width: 500, height: 28 }, {
    fontSize: 15,
    bold: true,
    color: "emerald-700",
    typeface: "Aptos",
  });
  addText(slide, `Sources: ${sources || "source exports"}`, { left: 802, top: 52, width: 410, height: 28 }, {
    fontSize: 12,
    color: "slate-500",
    typeface: "Aptos",
  });

  const y = 178;
  addCard(
    slide,
    "1. Reviews",
    "Play Store, App Store, Reddit, and Spotify Community exports filtered to English feedback from the last 12 months.",
    { left: 64, top: y, width: 330, height: 190 },
    "emerald-500",
  );
  addCard(
    slide,
    "2. AI Analysis",
    "Deduplication, sentiment, themes, intents, segments, and clusters convert raw comments into structured product signals.",
    { left: 475, top: y, width: 330, height: 190 },
    "sky-500",
  );
  addCard(
    slide,
    "3. Insights",
    "PM-ready findings, representative quotes, user segments, and opportunity areas for Spotify discovery surfaces.",
    { left: 886, top: y, width: 330, height: 190 },
    "fuchsia-500",
  );

  for (const left of [414, 825]) {
    addText(slide, "->", { left, top: y + 72, width: 40, height: 34 }, {
      fontSize: 30,
      bold: true,
      color: "slate-400",
      typeface: "Aptos",
    });
  }

  const statY = 438;
  const stats = [
    ["Top friction", topTheme.label, pct(topTheme.share)],
    ["Top intent", topIntent.label, pct(topIntent.share)],
    ["Top segment", topSegment.label, pct(topSegment.share)],
  ];
  for (let i = 0; i < stats.length; i += 1) {
    const left = 64 + i * 406;
    slide.shapes.add({
      geometry: "roundRect",
      position: { left, top: statY, width: 344, height: 126 },
      fill: "slate-900",
      line: { style: "solid", fill: "slate-900", width: 0 },
      borderRadius: "rounded-lg",
    });
    addText(slide, stats[i][0].toUpperCase(), { left: left + 24, top: statY + 20, width: 160, height: 18 }, {
      fontSize: 11,
      bold: true,
      color: "slate-300",
      typeface: "Aptos",
    });
    addText(slide, stats[i][2], { left: left + 238, top: statY + 18, width: 76, height: 46 }, {
      fontSize: 34,
      bold: true,
      color: "white",
      typeface: "Aptos Display",
    });
    addText(slide, stats[i][1], { left: left + 24, top: statY + 56, width: 250, height: 48 }, {
      fontSize: 20,
      bold: true,
      color: "white",
      typeface: "Aptos Display",
    });
  }

  addText(slide, "Output: PM report, cleaned review set, JSON summary, and editable one-slide workflow.", {
    left: 64,
    top: 640,
    width: 900,
    height: 24,
  }, { fontSize: 12, color: "slate-500", typeface: "Aptos" });

  const png = await presentation.export({ slide, format: "png", scale: 1 });
  await writeBlob(path.join(outputDir, "workflow_slide.png"), png);
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(path.join(outputDir, "workflow_slide.pptx"));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
