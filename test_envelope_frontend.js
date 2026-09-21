/* 容差包络前端无头冒烟测试（node + jsdom，开发用）。
 * 运行：node test_envelope_frontend.js
 * 依赖：/tmp/jsdomtest 下安装有 jsdom（npm i jsdom）
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("/tmp/jsdomtest/node_modules/jsdom");
const { execFileSync } = require("child_process");

const STATIC = path.join(__dirname, "app", "static");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const appjs = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

const failures = [];
function check(name, cond, detail = "") {
  console.log(`[${cond ? "PASS" : "FAIL"}] ${name}${!cond && detail ? "  -> " + detail : ""}`);
  if (!cond) failures.push(name);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const dom = new JSDOM(html, {
  url: "http://localhost:8000/",
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const { window } = dom;

function pyCall(endpoint, payload) {
  const script = `
import json, sys
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
payload = json.loads(sys.stdin.read())
r = c.post(${JSON.stringify(endpoint)}, json=payload)
sys.stdout.write(json.dumps({"status": r.status_code, "body": r.json()}, ensure_ascii=False))
`;
  const out = execFileSync("python3", ["-c", script], {
    input: JSON.stringify(payload),
    cwd: __dirname,
    encoding: "utf8",
  });
  return JSON.parse(out);
}

let envelopeNextError = null;
window.fetch = (url, opts) => {
  const payload = JSON.parse(opts.body);
  if (url === "/api/envelope" && envelopeNextError) {
    const err = envelopeNextError;
    envelopeNextError = null;
    return Promise.resolve({ ok: false, status: 422, json: () => Promise.resolve(err) });
  }
  const { status, body } = pyCall(url, payload);
  return Promise.resolve({ ok: status === 200, status, json: () => Promise.resolve(body) });
};

window.eval(appjs);
const doc = window.document;
const $ = (s) => doc.querySelector(s);

async function main() {
  // ---------- 0. 初始状态：包络表单禁用 ----------
  check("初始包络表单禁用",
    $("#env-quantity").disabled && $("#env-target").disabled &&
    $("#btn-envelope").disabled && $("#btn-env-add").disabled);
  check("初始无包络结果", $("#envelope-result").classList.contains("hidden"));

  // ---------- 1. 默认网络校核求解成功 → 装弹；改类型/目标 ----------
  $("#btn-solve").click();
  await sleep(80);
  check("求解成功后包络表单启用",
    !$("#env-quantity").disabled && !$("#btn-envelope").disabled && !$("#btn-env-add").disabled);
  check("目标节点下拉已填充",
    [...$("#env-target").options].map((o) => o.value).join(",") === "0,1");
  $("#env-quantity").value = "current";
  $("#env-quantity").dispatchEvent(new window.Event("change"));
  check("支路目标下拉包含 V1/R1",
    [...$("#env-target").options].map((o) => o.value).join(",") === "V1,R1");

  // 添加行、删除行、默认容差
  $("#btn-env-add").click();
  $("#btn-env-add").click();
  const rows = () => doc.querySelectorAll("#envelope-table tbody tr");
  check("添加两行容差电阻", rows().length === 2);
  check("第一行自动选中 R1（唯一电阻）", doc.querySelectorAll("#envelope-table tbody select")[0].value === "R1");
  check("默认容差 5%",
    doc.querySelectorAll("#envelope-table tbody input")[0].value === "5" &&
    doc.querySelectorAll("#envelope-table tbody input")[1].value === "5");
  rows()[1].querySelector("button").click();
  check("删除后剩一行", rows().length === 1);

  // ---------- 2. 电路编辑 → 立即失弹并清空结果 ----------
  const valueInput = doc.querySelector("#element-table tbody tr td:nth-child(5) input");
  valueInput.value = "12";
  valueInput.dispatchEvent(new window.Event("input"));
  check("改元件参数后包络表单失能",
    $("#btn-envelope").disabled && $("#btn-env-add").disabled &&
    doc.querySelectorAll("#envelope-table tbody input")[0].disabled);
  check("改元件参数后结果区隐藏", $("#envelope-result").classList.contains("hidden"));

  // ---------- 3. 载入示例①（分压器），求解，计算包络 ----------
  doc.querySelector('[data-sample="1"]').click();
  check("载入示例后失弹且容差行清空",
    $("#btn-envelope").disabled && doc.querySelectorAll("#envelope-table tbody tr").length === 0);
  $("#btn-solve").click();
  await sleep(80);
  check("示例①求解后重新装弹", !$("#btn-envelope").disabled);

  // 选两个电阻，各 10%，目标节点 2
  $("#btn-env-add").click();
  $("#btn-env-add").click();
  const sels = doc.querySelectorAll("#envelope-table tbody select");
  sels[0].value = "R1"; sels[0].dispatchEvent(new window.Event("change"));
  sels[1].value = "R2"; sels[1].dispatchEvent(new window.Event("change"));
  const tols = doc.querySelectorAll("#envelope-table tbody input");
  tols[0].value = "10"; tols[0].dispatchEvent(new window.Event("input"));
  tols[1].value = "10"; tols[1].dispatchEvent(new window.Event("input"));
  $("#env-quantity").value = "voltage";
  $("#env-quantity").dispatchEvent(new window.Event("change"));
  $("#env-target").value = "2";
  $("#env-target").dispatchEvent(new window.Event("change"));

  $("#btn-envelope").click();
  await sleep(120);
  check("包络结果区显示", !$("#envelope-result").classList.contains("hidden"));
  const meta = $("#envelope-meta").textContent;
  check("元信息：2 电阻 4 角点", meta.includes("2 个电阻") && meta.includes("4 个角点"), meta);
  check("角点表 4 行", doc.querySelectorAll("#envelope-corner-table tbody tr").length === 4);
  check("极值卡 2 张", doc.querySelectorAll("#envelope-extremes .env-ext-card").length === 2);
  check("SVG 包络图已绘制", doc.querySelectorAll("#envelope-chart svg").length === 1);
  check("min/max 为不同的行并分别高亮",
    doc.querySelectorAll("#envelope-corner-table tr.row-env-min").length === 1 &&
    doc.querySelectorAll("#envelope-corner-table tr.row-env-max").length === 1);
  // min 角点 +-（R1 上限 11，R2 下限 18），max 角点 -+
  const minCard = doc.querySelector(".env-ext-min");
  const maxCard = doc.querySelector(".env-ext-max");
  check("最小角点模式 +- 且列出阻值 11/18",
    minCard.textContent.includes("#1") && minCard.textContent.includes("+-") &&
    minCard.textContent.includes("11") && minCard.textContent.includes("18"),
    minCard.textContent.replace(/\s+/g, " "));
  check("最大角点模式 -+ 且列出阻值 9/22",
    maxCard.textContent.includes("#2") && maxCard.textContent.includes("-+") &&
    maxCard.textContent.includes("9") && maxCard.textContent.includes("22"),
    maxCard.textContent.replace(/\s+/g, " "));
  // 相对偏差为负/正（示例①含 I1/R3：min 偏差 -9.224%，max 偏差 +9.113%）
  const minCardNow = doc.querySelector(".env-ext-min");
  const maxCardNow = doc.querySelector(".env-ext-max");
  check("最小角点偏差为负、最大为正",
    minCardNow.textContent.includes("-9.224") &&
    maxCardNow.textContent.includes("9.1129"),
    maxCardNow.textContent.replace(/\s+/g, " "));
  // 角点表按值升序
  const vals = [...doc.querySelectorAll("#envelope-corner-table tbody tr td:nth-child(3)")]
    .map((td) => parseFloat(td.textContent));
  check("角点表按目标值升序", vals[0] <= vals[1] && vals[1] <= vals[2] && vals[2] <= vals[3],
    JSON.stringify(vals));

  // ---------- 4. 容差配置变化 → 旧结果清空（仍装弹）----------
  tols[0].value = "20";
  tols[0].dispatchEvent(new window.Event("input"));
  check("改容差后结果清空但仍装弹",
    $("#envelope-result").classList.contains("hidden") && !$("#btn-envelope").disabled);
  // 改目标类型同样清空
  $("#env-quantity").value = "current";
  $("#env-quantity").dispatchEvent(new window.Event("change"));
  check("改目标类型后结果清空", $("#envelope-result").classList.contains("hidden"));

  // ---------- 5. 前端校验：容差越界 ----------
  $("#env-target").value = "R1";
  $("#env-target").dispatchEvent(new window.Event("change"));
  tols[0].value = "60";
  tols[0].dispatchEvent(new window.Event("input"));
  $("#btn-envelope").click();
  await sleep(40);
  let fb = $("#envelope-feedback");
  check("越界容差前端拒绝并提示 0.01%～50%",
    !fb.classList.contains("hidden") && fb.textContent.includes("0.01%～50%"),
    fb.textContent.slice(0, 150));
  check("拒绝时无结果区", $("#envelope-result").classList.contains("hidden"));

  // ---------- 6. 后端 422（角点不可解）→ 结构化渲染并标出角点 ----------
  tols[0].value = "10";
  tols[0].dispatchEvent(new window.Event("input"));
  envelopeNextError = {
    ok: false,
    errors: [{
      code: "envelope_corner_unsolvable", stage: "corner",
      message: "电阻容差角点 1（+-）处网络无唯一解，无法确定完整的容差包络",
      items: [{
        ref: "角点 1（+-）", detail: "该角点对应的电阻取值组合无法求解",
        corner: {
          index: 1, pattern: "+-", sides: ["plus", "minus"],
          resistors: [
            { id: "R1", value: 11, side: "plus" },
            { id: "R2", value: 18, side: "minus" },
          ],
        },
        causes: [{ code: "singular", message: "线性方程组奇异", items: [] }],
      }],
    }],
  };
  $("#btn-envelope").click();
  await sleep(60);
  fb = $("#envelope-feedback");
  const txt = fb.textContent.replace(/\s+/g, " ");
  check("角点错误面板显示", !fb.classList.contains("hidden"));
  check("带阶段标签", txt.includes("容差角点求解"));
  check("标出角点编号/模式", txt.includes("角点 1") && txt.includes("+-"));
  check("列出角点电阻取值与上下限方向",
    txt.includes("R1") && txt.includes("11") && txt.includes("R2") && txt.includes("18") &&
    txt.includes("上限") && txt.includes("下限"));
  check("内联原始错误 causes", txt.includes("线性方程组奇异"));
  check("角点失败时结果区隐藏", $("#envelope-result").classList.contains("hidden"));

  // ---------- 7. baseline 阶段错误渲染 ----------
  envelopeNextError = {
    ok: false,
    errors: [{ code: "no_ground", stage: "baseline", message: "未指定接地节点", items: [] }],
  };
  $("#btn-envelope").click();
  await sleep(40);
  check("baseline 错误带阶段标签",
    $("#envelope-feedback").textContent.includes("基准网络求解") &&
    $("#envelope-feedback").textContent.includes("未指定接地节点"));

  // ---------- 8. 清空 → 彻底重置 ----------
  $("#btn-clear").click();
  check("清空后失弹", $("#btn-envelope").disabled && $("#btn-env-add").disabled);
  check("清空后容差行清空", doc.querySelectorAll("#envelope-table tbody tr").length === 0);
  check("清空后结果与反馈都隐藏",
    $("#envelope-result").classList.contains("hidden") &&
    $("#envelope-feedback").classList.contains("hidden"));

  console.log();
  if (failures.length) {
    console.error(`${failures.length} 个用例失败: ${failures.join("; ")}`);
    process.exit(1);
  }
  console.log("全部前端用例通过 ✔");
  process.exit(0);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
