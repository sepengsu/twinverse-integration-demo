/* Check real CAU artifacts, their displayed meaning, and the actual SKKU payload. */
const {chromium} = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const delay = ms => new Promise(resolve => setTimeout(resolve,ms));
(async () => {
  const output = path.resolve("runtime-verification/cau-result-ui");
  fs.mkdirSync(output,{recursive:true});
  const browser = await chromium.launch({channel:"chrome",headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1040,height:890}});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto("http://127.0.0.1:9000/dashboard?presentation=1");
    await page.waitForFunction(() => document.getElementById("target-agent").options.length >= 3);
    assert.equal(await page.locator("#workflow-mode").inputValue(),"direct");
    async function run(name) {
      const accepted = page.waitForResponse(r => r.url().endsWith("/dashboard/api/workflows") && r.request().method() === "POST");
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
        await delay(150);
      }
      assert.equal(workflow?.status,"completed",JSON.stringify(workflow?.state?.errors));
      await page.waitForFunction(id => document.getElementById("selected-run-label").textContent.includes(id) && !document.getElementById("launch").disabled,id);
      const events = snapshot.events.filter(event => event.global_task_id === id);
      fs.writeFileSync(path.join(output,`${name}.json`),JSON.stringify({workflow,events},null,2));
      return {workflow,events};
    }
    async function bounds(name) {
      const measurement = await page.evaluate(() => {
        const rect = selector => {const r=document.querySelector(selector).getBoundingClientRect(); return {top:r.top,bottom:r.bottom,height:r.height};};
        return {width:innerWidth,height:innerHeight,documentHeight:document.documentElement.scrollHeight,documentWidth:document.documentElement.scrollWidth,network:rect(".network-panel"),result:rect("#cau-result"),timeline:rect(".timeline-panel"),inspector:rect(".inspector-panel"),json:rect("#payload")};
      });
      for (const panel of [measurement.network,measurement.result,measurement.timeline,measurement.inspector]) assert(panel.top >= 0 && panel.bottom <= measurement.height,JSON.stringify(measurement));
      assert(measurement.timeline.height >= 180,JSON.stringify(measurement));
      assert(measurement.json.height >= 120,JSON.stringify(measurement));
      assert(measurement.documentWidth <= measurement.width && measurement.documentHeight <= measurement.height,JSON.stringify(measurement));
      await page.screenshot({path:path.join(output,`${name}.png`)});
      return measurement;
    }
    await page.locator("#target-agent").selectOption("cau_process");
    await page.locator("#capability").selectOption("process_planning");
    await page.locator("#open-settings").click();
    await page.locator("#part").fill("skku/workpiece_01");
    await page.locator("#destination").fill("skku/work_table_01");
    assert(await page.locator("#unity-preview").isDisabled());
    await page.locator("#close-settings").click();
    const cau = await run("cau-alone");
    const plan = cau.workflow.state.artifacts.process_plan;
    assert(plan && !cau.workflow.state.artifacts.robot_plan && !cau.workflow.unity);
    assert(!cau.events.some(e => e.target === "skku_robot"));
    assert.equal(await page.locator(".cau-operation").count(),plan.operations.length);
    assert((await page.locator("#cau-estimate").textContent()).includes(String(plan.operations.reduce((sum,op) => sum+op.estimated_duration_seconds,0))));
    assert((await page.locator("#cau-estimate").textContent()).includes("Dummy 예제"));
    assert.equal(await page.locator("#cau-machine").textContent(),plan.machine_id);
    assert.equal(await page.locator("#handoff-status").textContent(),"CAU 단독 실행");
    assert.equal(await page.locator("#cau-handoff.linked").count(),0);
    await page.locator("#show-cau-plan").click();
    assert.deepEqual(JSON.parse(await page.locator("#payload").textContent()),plan);
    assert(await page.locator("#edge-cau.focus-response").count());
    const soloBounds = await bounds("cau-alone");

    await page.locator("#workflow-mode").selectOption("process_then_robot");
    await page.locator("#open-settings").click();
    await page.locator("#unity-preview").check();
    await page.locator("#close-settings").click();
    const chain = await run("collaboration");
    const processPlan = chain.workflow.state.artifacts.process_plan;
    const post = chain.events.find(e => e.kind === "http_request" && e.target === "skku_robot" && e.data.method === "POST");
    const request = post.data.body.message.parts.find(p => p.data).data;
    assert.equal(request.input.destination,processPlan.machine_id);
    assert.equal(await page.locator("#skku-destination").textContent(),request.input.destination);
    assert.equal(await page.locator("#cau-machine").textContent(),processPlan.machine_id);
    assert(await page.locator("#cau-handoff.linked").count());
    assert.equal(await page.locator("#handoff-status").textContent(),"✓ 동일 설비 전송 확인");
    assert.equal(chain.workflow.unity.playback_kind,"joint_waypoint_simulation");
    assert.equal(chain.workflow.unity.accepted_command_ids.length,5);
    assert.equal(chain.workflow.unity.waypoint_count,4);
    await page.locator("#show-cau-plan").click();
    assert.deepEqual(JSON.parse(await page.locator("#payload").textContent()),processPlan);
    const chainBounds = await bounds("collaboration");
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),"Mobile horizontal overflow");
    await page.screenshot({path:path.join(output,"mobile.png"),fullPage:true});
    assert.deepEqual(errors,[]);
    const result = {status:"passed",cau_task:cau.workflow.global_task_id,collaboration_task:chain.workflow.global_task_id,bounds:{cau:soloBounds,collaboration:chainBounds},console_errors:errors};
    fs.writeFileSync(path.join(output,"result.json"),JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
