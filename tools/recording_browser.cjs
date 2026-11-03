/* Dedicated browser window for the requested recording; accepts local cues. */
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const directory = path.resolve(process.env.A2A_RECORDING_DIRECTORY || "recordings/a2a-unity");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  fs.mkdirSync(directory, {recursive:true});
  const browser = await chromium.launch({channel:"chrome",headless:false,args:["--window-position=0,0","--window-size=1070,970"]});
  const context = await browser.newContext({viewport:{width:1040,height:890},deviceScaleFactor:1});
  const page = await context.newPage();
  const submittedRequests = [];
  page.on("request", request => {
    if (request.url().endsWith("/dashboard/api/workflows") && request.method() === "POST") {
      submittedRequests.push({time:Date.now(),body:request.postDataJSON()});
      fs.writeFileSync(path.join(directory,"submitted-requests.json"),JSON.stringify(submittedRequests,null,2));
    }
  });
  await page.goto("http://127.0.0.1:9000/dashboard?presentation=1");
  await page.getByText("LIVE · 실시간 연결", {exact:true}).waitFor();
  await page.waitForFunction(() => document.getElementById("stat-agents").textContent === "3");
  await page.screenshot({path:path.join(directory,"browser-preview.png")});
  fs.writeFileSync(path.join(directory,"browser-ready.json"),JSON.stringify({ready:true,title:await page.title(),url:page.url()}));
  async function configure(mode, agentId="skku_robot", capability="robot_path_planning") {
    await page.locator("#workflow-mode").selectOption(mode);
    if (mode === "direct") {
      await page.locator("#target-agent").selectOption(agentId);
      await page.locator("#capability").selectOption(capability);
    }
    if (!await page.locator("#launch-settings").evaluate(element => element.open)) await page.locator("#open-settings").click();
    await page.locator("#part").fill("skku/workpiece_01");
    if (!await page.locator("#source").isDisabled()) await page.locator("#source").fill("skku/workpiece_01");
    await page.locator("#destination").fill("skku/work_table_01");
    // Agent/capability selection clears the optional JSON through the form's
    // change handlers; the textarea may be inside collapsed details.
    if (!await page.locator("#unity-preview").isDisabled()) await page.locator("#unity-preview").check();
    await page.locator("#close-settings").click();
  }
  async function showField(field) {
    await page.locator("#payload").evaluate((element, name) => {
      const line = element.textContent.split("\n").findIndex(row => row.includes(`"${name}"`));
      element.scrollTop = Math.max(0,line - 1) * parseFloat(getComputedStyle(element).lineHeight);
    },field);
  }
  const initialCommand = path.join(directory,"browser-command.json");
  let previous = fs.existsSync(initialCommand) ? fs.readFileSync(initialCommand,"utf8") : "";
  while (true) {
    const file = path.join(directory,"browser-command.json");
    if (!fs.existsSync(file)) { await delay(250); continue; }
    const raw = fs.readFileSync(file,"utf8");
    if (raw === previous) { await delay(250); continue; }
    previous = raw;
    const command = JSON.parse(raw);
    try {
      if (command.action === "stop") { await browser.close(); break; }
      if (command.action === "reload") await page.reload();
      if (command.action === "overview") await page.evaluate(() => window.scrollTo({top:0,behavior:"smooth"}));
      if (command.action === "launch-form") await page.locator("#open-settings").click();
      if (command.action === "close-settings") await page.locator("#close-settings").click();
      if (command.action === "prepare-direct") await configure("direct");
      if (command.action === "prepare-cau") await configure("direct","cau_process","process_planning");
      if (command.action === "prepare-pilot") await configure("process_then_robot");
      if (command.action === "prepare-single") {
        await configure("process_then_robot");
        await page.locator("#open-settings").click();
        await page.locator("#goal").fill("");
      }
      if (command.action === "live-follow" && await page.locator("#follow-live").getAttribute("aria-pressed") !== "true") await page.locator("#follow-live").click();
      if (command.action === "execution") await page.locator("#execution-summary").scrollIntoViewIfNeeded();
      if (command.action === "robot-plan") { await page.locator('[data-tab="artifacts"]').click(); await page.locator('[data-artifact="robot_plan"]').click(); }
      if (command.action === "process-plan") await page.locator("#show-cau-plan").click();
      if (command.action === "process-operations") { await page.locator("#show-cau-plan").click(); await showField("operations"); }
      if (command.action === "handoff") { await page.locator(".event-row").filter({hasText:"로봇 작업으로 변환"}).click(); await showField("destination"); }
      if (command.action === "unity-result") { await page.locator('[data-tab="artifacts"]').click(); await page.locator('[data-artifact="unity_preview"]').click(); }
      if (command.action === "validation") {
        await page.locator('[data-tab="artifacts"]').click();
        await page.locator('[data-artifact="robot_plan"]').click();
        await page.locator("#payload").evaluate(element => {
          const line = element.textContent.split("\n").findIndex(row => row.includes('"validation"'));
          element.scrollTop = Math.max(0,line) * parseFloat(getComputedStyle(element).lineHeight);
        });
      }
      if (["run","start","start-direct","start-pilot","start-cau","submit-once"].includes(command.action)) {
        if (command.action === "submit-once") {
          const inputStarted = Date.now();
          if (!await page.locator("#launch-settings").evaluate(element => element.open)) throw new Error("Single input form must already be open");
          await page.locator("#goal").pressSequentially("공정 계획과 경로를 생성하고 Unity Simulation으로 로봇을 수행합니다.",{delay:20});
          await delay(Math.max(0,3000-(Date.now()-inputStarted)));
          await page.locator("#close-settings").click();
          fs.writeFileSync(path.join(directory,"single-input.json"),JSON.stringify({input_seconds:(Date.now()-inputStarted)/1000,simulation_requested:await page.locator("#unity-preview").isChecked()}));
        }
        else if (command.action === "start-cau") await configure("direct","cau_process","process_planning");
        else await configure(command.action === "start-direct" ? "direct" : "process_then_robot");
        const accepted = page.waitForResponse(response => response.url().endsWith("/dashboard/api/workflows") && response.request().method() === "POST");
        await page.locator("#launch").click();
        const response = await accepted;
        if (response.status() !== 202) throw new Error(`Workflow rejected: ${response.status()}`);
        const {global_task_id:taskId} = await response.json();
        fs.writeFileSync(path.join(directory,"filmed-task.json"),JSON.stringify({global_task_id:taskId}));
        await page.locator("#execution-summary").scrollIntoViewIfNeeded();
        if (command.action.startsWith("start") || command.action === "submit-once") {
          fs.writeFileSync(path.join(directory,"browser-ack.json"),JSON.stringify({id:command.id,action:command.action,status:"ok"}));
          continue;
        }
        let snapshot, workflow;
        const deadline = Date.now() + 60000;
        while (Date.now() < deadline) {
          snapshot = await page.evaluate(async () => (await fetch("/dashboard/api/snapshot")).json());
          workflow = snapshot.workflows.find(row => row.global_task_id === taskId);
          if (workflow && ["completed","failed","input_required"].includes(workflow.status)) break;
          await delay(250);
        }
        if (!workflow || workflow.status === "working") throw new Error(`Workflow ${taskId} timed out`);
        fs.writeFileSync(path.join(directory,"filmed-workflow.json"),JSON.stringify(workflow,null,2));
        fs.writeFileSync(path.join(directory,"filmed-events.json"),JSON.stringify(snapshot.events.filter(event => event.global_task_id === taskId),null,2));
        if (workflow.status !== "completed") throw new Error(`Workflow ${taskId}: ${workflow.status}`);
      }
      if (command.action === "timeline") await page.locator(".section-heading").scrollIntoViewIfNeeded();
      if (command.action === "cau-request") await page.locator(".event-row").filter({hasText:"A2A 메시지 요청"}).first().click();
      if (command.action === "cau-response") await page.locator(".event-row").filter({hasText:"A2A 작업 결과"}).first().click();
      if (command.action === "skku-response") await page.locator(".event-row").filter({hasText:"A2A 작업 결과"}).last().click();
      if (command.action === "skku-request") await page.locator(".event-row").filter({hasText:"Supervisor → 성균관대 · A2A 메시지 요청"}).last().click();
      if (command.action === "skku-destination") { await page.locator(".event-row").filter({hasText:"Supervisor → 성균관대 · A2A 메시지 요청"}).last().click(); await showField("destination"); }
      if (command.action === "artifacts") await page.locator('[data-tab="artifacts"]').click();
      if (command.action === "state") await page.locator('[data-tab="state"]').click();
      if (command.action === "agent-card") await page.locator("#agent-cau_process").click();
      await page.screenshot({path:path.join(directory,"browser-preview.png")});
      fs.writeFileSync(path.join(directory,"browser-ack.json"),JSON.stringify({id:command.id,action:command.action,status:"ok"}));
    } catch (error) {
      fs.writeFileSync(path.join(directory,"browser-ack.json"),JSON.stringify({id:command.id,action:command.action,status:"error",error:error.message}));
    }
  }
})().catch(error => { console.error(error);process.exitCode=1; });
