/* Verify direct agent/skill execution and the optional pilot in a real browser. */
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");

(async () => {
  const output = path.resolve("runtime-verification/direct-mode-ui");
  fs.mkdirSync(output, {recursive:true});
  const browser = await chromium.launch({channel:"chrome", headless:true});
  try {
    const context = await browser.newContext({viewport:{width:1560,height:1100},acceptDownloads:true,permissions:["clipboard-read","clipboard-write"]});
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(process.argv[2] || "http://127.0.0.1:9000/dashboard");
    await page.getByText("LIVE · 실시간 연결", {exact:true}).waitFor();
    await page.waitForFunction(() => document.getElementById("target-agent").options.length >= 3);
    assert.equal(await page.locator("#workflow-mode").inputValue(), "direct");
    assert.equal(await page.locator("#target-agent").inputValue(), "skku_robot");
    assert.equal(await page.locator("#capability").inputValue(), "robot_path_planning");
    await page.screenshot({path:path.join(output,"default-desktop.png"),fullPage:true});

    async function run(name) {
      if (await page.locator("#launch-settings").evaluate(element => element.open)) await page.locator("#close-settings").click();
      const accepted = page.waitForResponse(response => response.url().endsWith("/dashboard/api/workflows") && response.request().method() === "POST");
      await page.locator("#launch").click();
      const response = await accepted;
      assert.equal(response.status(),202);
      const {global_task_id:id} = await response.json();
      let snapshot, workflow;
      const deadline = Date.now() + 95000;
      while (Date.now() < deadline) {
        snapshot = await page.evaluate(async () => (await fetch("/dashboard/api/snapshot")).json());
        workflow = snapshot.workflows.find(row => row.global_task_id === id);
        if (["completed","failed","input_required"].includes(workflow?.status)) break;
        await new Promise(resolve => setTimeout(resolve,200));
      }
      assert.equal(workflow?.status,"completed",JSON.stringify(workflow?.state?.errors));
      const events = snapshot.events.filter(row => row.global_task_id === id);
      fs.writeFileSync(path.join(output,`${name}.json`),JSON.stringify({workflow,events},null,2));
      await page.waitForFunction(() => !document.getElementById("launch").disabled);
      return {workflow,events};
    }

    await page.locator("#target-agent").selectOption("cau_process");
    await page.locator("#capability").selectOption("process_information");
    assert(await page.locator("#unity-preview").isDisabled());
    assert.equal(await page.locator("#unity-preview").isChecked(), false);
    await page.locator("#open-settings").click();
    await page.locator("#part").fill("");
    await page.locator("#direct-input").evaluate(element => element.open = true);
    await page.locator("#capability-input").fill('{"topic":"materials"}');
    const info = await run("cau-information");
    assert.equal(Object.keys(info.workflow.state.tasks).length,1);
    assert(info.workflow.state.artifacts.process_information);
    assert(!info.workflow.state.artifacts.robot_plan);
    assert.equal(info.events.filter(event => event.kind === "http_request" && event.data.method === "POST").length,1);
    assert(info.events.filter(event => event.kind === "delegation_started").every(event => event.target === "cau_process"));
    await page.screenshot({path:path.join(output,"cau-information.png"),fullPage:true});
    console.log("CAU information direct execution passed");

    await page.locator("#workflow-mode").selectOption("process_then_robot");
    assert.equal(await page.locator("#direct-agent-field").isVisible(),false);
    await page.locator("#open-settings").click();
    await page.locator("#unity-preview").uncheck();
    await page.locator("#part").fill("skku/workpiece_01");
    const pilot = await run("optional-pilot");
    assert.equal(Object.keys(pilot.workflow.state.tasks).length,2);
    assert(pilot.workflow.state.artifacts.process_plan && pilot.workflow.state.artifacts.robot_plan);
    assert(pilot.events.some(event => event.kind === "payload_transformed"));
    await page.screenshot({path:path.join(output,"optional-pilot.png"),fullPage:true});
    console.log("Optional process-to-robot workflow passed");

    await page.locator("#workflow-mode").selectOption("direct");
    await page.locator("#target-agent").selectOption("skku_robot");
    assert.equal(await page.locator("#capability").inputValue(),"robot_path_planning");
    assert.equal(await page.locator("#capability-input").inputValue(),"{}");
    await page.locator("#open-settings").click();
    await page.locator("#unity-preview").check();
    const direct = await run("direct-robot-unity");
    assert.equal(Object.keys(direct.workflow.state.tasks).length,1);
    assert.equal(direct.workflow.state.tasks.T1.assigned_agent_id,"skku_robot");
    assert(!direct.workflow.state.artifacts.process_plan);
    assert.equal(direct.workflow.unity.status,"completed");
    assert.equal(direct.workflow.unity.waypoint_count,4);
    assert.equal(direct.workflow.unity.accepted_command_ids.length,5);
    assert.equal(direct.workflow.unity.playback_kind,"joint_waypoint_simulation");
    assert(!direct.events.some(event => event.target === "cau_process"));
    assert(!direct.events.some(event => event.kind === "payload_transformed"));
    await page.locator('[data-tab="artifacts"]').click();
    await page.locator('[data-artifact="robot_plan"]').click();
    let payload = JSON.parse(await page.locator("#payload").textContent());
    assert.equal(payload.validation.collision_free,null);
    await page.locator('[data-filter="requests"]').click();
    assert.equal(await page.locator(".event-row").count(),2);
    await page.locator('[data-filter="all"]').click();
    await page.locator("#copy").click();
    await page.getByText("JSON을 복사했습니다.",{exact:true}).waitFor();
    const downloadEvent = page.waitForEvent("download");
    await page.locator("#export").click();
    await (await downloadEvent).saveAs(path.join(output,"exported-trace.json"));
    await page.screenshot({path:path.join(output,"direct-robot-desktop.png"),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(output,"mobile.png"),fullPage:true});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),"Mobile overflow");
    await page.reload();
    await page.getByText("LIVE · 실시간 연결", {exact:true}).waitFor();
    await page.waitForFunction(() => document.querySelectorAll(".event-row").length > 0);
    assert.equal(await page.locator("#workflow-mode").inputValue(),"direct");
    assert.deepEqual(errors,[]);
    const result = {status:"passed",default_mode:"direct",cau_info:info.workflow.global_task_id,optional_pilot:pilot.workflow.global_task_id,direct_robot:direct.workflow.global_task_id,unity:direct.workflow.unity.playback_kind,console_errors:errors};
    fs.writeFileSync(path.join(output,"result.json"),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
