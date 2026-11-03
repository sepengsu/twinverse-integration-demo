/* Verify the shared viewport and routing against the running A2A services. */
const {chromium} = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const delay = ms => new Promise(resolve => setTimeout(resolve,ms));
(async () => {
  const output = path.resolve("runtime-verification/communication-view");
  fs.mkdirSync(output,{recursive:true});
  const browser = await chromium.launch({channel:"chrome",headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1040,height:890}});
    const errors = [], bounds = [];
    page.on("pageerror",error => errors.push(error.message));
    await page.goto("http://127.0.0.1:9000/dashboard");
    await page.waitForFunction(() => document.getElementById("target-agent").options.length >= 3);
    await page.getByText("LIVE · 실시간 연결",{exact:true}).waitFor();
    async function checkBounds(name) {
      const measurement = await page.evaluate(() => {
        const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return {top:r.top,bottom:r.bottom,width:r.width,height:r.height}; };
        return {width:innerWidth,height:innerHeight,documentHeight:document.documentElement.scrollHeight,documentWidth:document.documentElement.scrollWidth,network:rect(".network-panel"),timeline:rect(".timeline-panel"),inspector:rect(".inspector-panel")};
      });
      assert(measurement.network.height > 100,`${name}: hidden network`);
      assert(measurement.timeline.height >= 180,`${name}: timeline too small ${measurement.timeline.height}`);
      for (const panel of [measurement.network,measurement.timeline,measurement.inspector]) {
        assert(panel.top >= 0 && panel.bottom <= measurement.height,`${name}: panel outside viewport ${JSON.stringify(measurement)}`);
      }
      assert(measurement.documentHeight <= measurement.height,`${name}: document scroll`);
      assert(measurement.documentWidth <= measurement.width,`${name}: horizontal scroll`);
      bounds.push({name,...measurement});
      await page.screenshot({path:path.join(output,`${name}.png`)});
    }
    await checkBounds("default-1040");
    await page.locator("#presentation").click();
    await checkBounds("filming-1040");
    await page.setViewportSize({width:1560,height:1000});
    await checkBounds("filming-1560");
    await page.locator("#presentation").click();
    await page.setViewportSize({width:1040,height:890});
    assert.equal(await page.locator("#workflow-mode").inputValue(),"direct");

    async function run(name, expectUnity=false) {
      const accepted = page.waitForResponse(r => r.url().endsWith("/dashboard/api/workflows") && r.request().method() === "POST");
      await page.locator("#launch").click();
      const response = await accepted;
      assert.equal(response.status(),202);
      const {global_task_id:id} = await response.json();
      let workflow, snapshot, sawUnity = false, capturedUnity = false;
      const deadline = Date.now() + 95000;
      while (Date.now() < deadline) {
        snapshot = await page.evaluate(async () => (await fetch("/dashboard/api/snapshot")).json());
        workflow = snapshot.workflows.find(row => row.global_task_id === id);
        if (await page.locator("#activity-agent").textContent() === "Unity 확인·재생 중") {
          sawUnity = true;
          assert((await page.locator("#response-agent").textContent()).includes("성균관대 → Supervisor"));
          assert(!(await page.locator("#agent-skku_robot").getAttribute("class")).split(" ").includes("working"),"Unity replay must not label the agent as working");
          if (!capturedUnity && workflow?.unity?.status === "running") { await page.screenshot({path:path.join(output,"unity-running.png")}); capturedUnity = true; }
        }
        if (["completed","failed","input_required"].includes(workflow?.status)) break;
        await delay(150);
      }
      assert.equal(workflow?.status,"completed",JSON.stringify(workflow?.state?.errors));
      await page.waitForFunction(() => document.getElementById("activity-agent").textContent.includes("완료"));
      if (expectUnity) assert(sawUnity,"Missing real Unity replay state");
      const events = snapshot.events.filter(event => event.global_task_id === id);
      fs.writeFileSync(path.join(output,`${name}.json`),JSON.stringify({workflow,events},null,2));
      await checkBounds(name);
      return {workflow,events};
    }
    await page.locator("#target-agent").selectOption("cau_process");
    await page.locator("#capability").selectOption("process_information");
    await page.locator("#open-settings").click();
    await page.locator("#direct-input").evaluate(e => e.open = true);
    await page.locator("#capability-input").fill('{"topic":"materials"}');
    await page.locator("#close-settings").click();
    const cau = await run("cau-response");
    assert.equal(await page.locator("#response-agent").textContent(),"중앙대 → Supervisor");
    assert((await page.locator("#detail-title").textContent()).includes("중앙대 → Supervisor"));
    assert((await page.locator("#agent-skku_robot .node-state").textContent()).includes("참여 안 함"));
    assert(await page.locator("#edge-cau.focus-response").count());
    const request = cau.events.find(e => e.kind === "http_request" && e.data.method === "POST");
    const reply = cau.events.find(e => e.kind === "task_received");
    await page.locator(`[data-event="${request.id}"]`).click();
    assert.equal(await page.locator("#follow-live").getAttribute("aria-pressed"),"false");
    assert(await page.locator("#edge-cau.focus-request").count());
    assert((await page.locator("#communication-focus").textContent()).includes("Supervisor → 중앙대"));
    await page.waitForResponse(r => r.url().endsWith("/dashboard/api/snapshot"));
    assert(await page.locator("#edge-cau.focus-request").count(),"Manual selection overwritten by live refresh");
    await page.screenshot({path:path.join(output,"selected-request.png")});
    await page.locator(`[data-event="${reply.id}"]`).click();
    assert(await page.locator("#edge-cau.focus-response").count());
    await page.locator("#follow-live").click();
    assert.equal(await page.locator("#follow-live").getAttribute("aria-pressed"),"true");
    assert.equal(Number(await page.locator(".event-row.selected").getAttribute("data-event")),reply.id);

    await page.locator("#target-agent").selectOption("skku_robot");
    await page.locator("#capability").selectOption("robot_path_planning");
    await page.locator("#open-settings").click();
    await page.locator("#unity-preview").check();
    await page.locator("#close-settings").click();
    const skku = await run("skku-response",true);
    assert.equal(await page.locator("#response-agent").textContent(),"성균관대 → Supervisor");
    assert(await page.locator("#edge-skku.focus-response").count());
    assert(["joint_waypoint_simulation","tcp_path_preview"].includes(skku.workflow.unity.playback_kind));
    assert.equal(await page.locator("#activity-agent").textContent(),skku.workflow.unity.playback_kind === "joint_waypoint_simulation" ? "관절 시뮬레이션 완료" : "TCP 경로 재생 완료");
    assert.equal(skku.workflow.unity.accepted_command_ids.length,5);
    assert.equal(skku.workflow.unity.waypoint_count,4);
    assert(!skku.events.some(e => e.source === "cau_process" || e.target === "cau_process"));
    await page.setViewportSize({width:1920,height:1080});
    await checkBounds("desktop-1920");
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),"Mobile horizontal overflow");
    await page.screenshot({path:path.join(output,"mobile.png"),fullPage:true});
    assert.deepEqual(errors,[]);
    const result = {status:"passed",bounds,cau_task:cau.workflow.global_task_id,skku_task:skku.workflow.global_task_id,console_errors:errors};
    fs.writeFileSync(path.join(output,"result.json"),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
