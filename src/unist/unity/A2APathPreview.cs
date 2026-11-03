using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Twinverse.UnityRuntime;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.SceneManagement;

// Twinverse가 가진 경유점을 작업 ID별로 확인하고 Unity의 대상 로봇에서 재생합니다.
// 이 컴포넌트는 MQTT 이동 명령이나 실물 로봇 게이트웨이를 직접 호출하지 않습니다.
public sealed class A2APathPreview : MonoBehaviour
{
    const string Api = "http://127.0.0.1:9000/dashboard/api/unity";
    readonly HashSet<string> processed = new HashSet<string>();
    GameObject visuals;
    string task = "Awaiting a new A2A task";
    string stage = "READY";
    string detail = "Live Twinverse MQTT waypoints / collision check not performed";
    float progress;
    int accepted;
    int expected;
    int pointsCount;
    string playbackKind = "tcp_path_preview";
    string sessionId;

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    static void Boot()
    {
        // 장면이 로드되면 브리지를 하나만 만들고 Supervisor 작업을 기다립니다.
        if (FindFirstObjectByType<A2APathPreview>() != null) return;
        var go = new GameObject("A2A Path Preview Bridge");
        go.AddComponent<A2APathPreview>();
    }

    void OnEnable()
    {
        ClearVisuals();
        Application.runInBackground = true;
        foreach (var behaviour in FindObjectsByType<MonoBehaviour>(FindObjectsInactive.Include, FindObjectsSortMode.None))
            if (behaviour.GetType().Name == "SkkuGp25RosbridgeGateway") behaviour.enabled = false;
        StartCoroutine(Poll());
    }

    void OnDisable() { StopAllCoroutines(); }

    void ClearVisuals()
    {
        foreach (var item in FindObjectsByType<Transform>(FindObjectsInactive.Include, FindObjectsSortMode.None))
            if (item.name == "A2A authoritative path") Destroy(item.gameObject);
        visuals = null;
    }

    IEnumerator Poll()
    {
        // 로컬 Supervisor에서 대기 작업을 주기적으로 가져옵니다.
        while (true)
        {
            using (var request = UnityWebRequest.Get(Api + "/job?scene=" + UnityWebRequest.EscapeURL(SceneManager.GetActiveScene().name)))
            {
                request.timeout = 3;
                yield return request.SendWebRequest();
                if (request.result == UnityWebRequest.Result.Success)
                {
                    var envelope = JsonUtility.FromJson<Envelope>(request.downloadHandler.text);
                    if (envelope != null && !string.IsNullOrEmpty(envelope.session_id) && sessionId != envelope.session_id)
                    {
                        sessionId = envelope.session_id;
                        ClearVisuals();
                        stage = "READY";
                        task = "Awaiting a new A2A task";
                        accepted = expected = pointsCount = 0;
                        progress = 0;
                        detail = "Local target robot simulation / collision check not performed";
                        ResetPreviewPose();
                    }
                    if (envelope != null && envelope.job != null && !string.IsNullOrEmpty(envelope.job.global_task_id)
                        && processed.Add(envelope.job.global_task_id))
                        yield return Playback(envelope.job);
                }
            }
            yield return new WaitForSecondsRealtime(.75f);
        }
    }

    static void ResetPreviewPose()
    {
        // 매번 관측된 로봇 자세에서 시작합니다. 이전 재생의 마지막 자세를 쓰면
        // 다음 작업의 역기구학(IK)이 다른 해로 이어질 수 있습니다.
        var target = TwinObjectBinding.FindByObjectId("skku/gp25_target");
        var actual = TwinObjectBinding.FindByObjectId("skku/gp25");
        var targetAdapter = target != null ? target.TargetTransform.GetComponent<UrdfRobotTwinAdapter>() : null;
        var actualAdapter = actual != null ? actual.TargetTransform.GetComponent<UrdfRobotTwinAdapter>() : null;
        if (targetAdapter != null && actualAdapter != null && actualAdapter.TryGetJointAnglesDeg(out var angles))
            targetAdapter.SetJointAnglesDeg(angles);
    }

    IEnumerator Playback(Job job)
    {
        task = job.global_task_id;
        stage = "WAITING FOR THIS TASK'S COMMANDS";
        expected = job.expected_command_ids.Length;
        accepted = 0;
        pointsCount = 0;
        progress = 0;
        ClearVisuals();
        var authority = FindFirstObjectByType<SceneAuthorityController>();
        // Twinverse 0.1에는 명령별 승인 조회용 공개 API가 없습니다.
        // 실제 승인 ID를 보관하는 내부 기록을 읽고, 접근할 수 없으면 승인으로 간주하지 않습니다.
        var ledgerField = typeof(SceneAuthorityController).GetField("acceptedCommandIds", BindingFlags.Instance | BindingFlags.NonPublic);
        var deadline = Time.realtimeSinceStartup + 20;
        TwinObjectBinding binding = null;
        TwinverseRobotPathStateSnapshot snapshot = null;
        while (Time.realtimeSinceStartup < deadline)
        {
            var ledger = authority != null && ledgerField != null ? ledgerField.GetValue(authority) as HashSet<string> : null;
            accepted = ledger == null ? 0 : job.expected_command_ids.Count(id => ledger.Contains(id));
            binding = TwinObjectBinding.FindByObjectId(job.object_id);
            if (binding != null) snapshot = TwinverseRobotPathState.Read(binding);
            if (accepted == expected && authority.LastAcceptedCommandId == job.expected_command_ids[expected - 1]
                && snapshot != null && snapshot.WaypointCount == job.expected_waypoint_count) break;
            if (authority != null && job.expected_command_ids.Contains(authority.LastRejectedCommandId))
            {
                stage = "REJECTED";
                yield return Report(job, "failed", Array.Empty<Vector3>(), "Unity rejected a command: " + authority.LastRejectionReason);
                yield break;
            }
            yield return new WaitForSecondsRealtime(.1f);
        }
        if (accepted != expected || snapshot == null || snapshot.WaypointCount != job.expected_waypoint_count)
        {
            stage = "FAILED";
            yield return Report(job, "failed", Array.Empty<Vector3>(), "Exact command receipts or authoritative waypoint count did not match.");
            yield break;
        }
        var worldPoints = snapshot.WaypointPositionsLocal.Select(position => binding.TargetTransform.TransformPoint(position)).ToArray();
        pointsCount = worldPoints.Length;
        ResetPreviewPose();
        var adapter = binding.TargetTransform.GetComponent<UrdfRobotTwinAdapter>() ?? binding.GetComponent<UrdfRobotTwinAdapter>() ?? binding.TargetTransform.GetComponentInChildren<UrdfRobotTwinAdapter>(true);
        if (adapter != null) adapter.AutoBindAxes();
        Debug.Log($"A2A preview adapter binding={binding.name} target={binding.TargetTransform.name} adapter={(adapter != null ? adapter.name : "none")} chain={(adapter != null && adapter.IsKinematicChainReady)} tcp={(adapter != null && adapter.ToolCenterPoint != null ? adapter.ToolCenterPoint.name : "none")}");
        var jointPlans = new List<float[]>();
        bool jointReady = adapter != null && adapter.IsKinematicChainReady && adapter.ToolCenterPoint != null;
        float[] initialAngles = null;
        if (jointReady) jointReady = adapter.TryGetJointAnglesDeg(out initialAngles);
        for (int index = 0; jointReady && index < worldPoints.Length; index++)
        {
            var rotation = index < snapshot.WaypointRotationsLocal.Count
                ? binding.TargetTransform.rotation * snapshot.WaypointRotationsLocal[index]
                : adapter.ToolCenterPoint.rotation;
            jointReady = TryPlanWaypoint(adapter, worldPoints[index], rotation, initialAngles, index, out var angles, out var error, out var angleError);
            Debug.Log($"A2A preview IK task={task} waypoint={index} solved={jointReady} position_error={error} rotation_error={angleError}");
            if (jointReady) { jointPlans.Add(angles); adapter.SetJointAnglesDeg(angles); }
            yield return null;
        }
        if (adapter != null && initialAngles != null) adapter.SetJointAnglesDeg(initialAngles);
        playbackKind = jointReady ? "joint_waypoint_simulation" : "tcp_path_preview";
        stage = "ACCEPTED";
        detail = jointReady ? "Local joint waypoint simulation / collision unchecked" : "TCP path replay / joint IK not verified / collision unchecked";
        BuildVisuals(worldPoints);
        FrameCamera(worldPoints);
        yield return Report(job, "accepted", worldPoints, "This task's MQTT commands and four Unity waypoints were confirmed.");
        yield return new WaitForSecondsRealtime(1.2f);
        var marker = visuals.transform.Find("TCP preview");
        var start = jointReady ? adapter.ToolCenterPoint.position : worldPoints[0];
        var path = new List<Vector3> {start};
        path.AddRange(worldPoints);
        float duration = 12f;
        if (float.TryParse(Environment.GetEnvironmentVariable("A2A_DEMO_SIMULATION_SECONDS"),
            System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var demoDuration)
            && !float.IsNaN(demoDuration) && !float.IsInfinity(demoDuration))
            duration = Mathf.Clamp(demoDuration, 3f, 60f);
        Debug.Log($"A2A preview playback task={task} duration_seconds={duration}");
        float elapsed = 0;
        float lastReport = -1;
        stage = "PLAYING";
        while (elapsed < duration)
        {
            elapsed += Time.unscaledDeltaTime;
            progress = Mathf.Clamp01(elapsed / duration);
            float scaled = progress * worldPoints.Length;
            int segment = Mathf.Min(worldPoints.Length - 1, Mathf.FloorToInt(scaled));
            float fraction = Mathf.Clamp01(scaled - segment);
            if (jointReady)
            {
                var from = segment == 0 ? initialAngles : jointPlans[segment - 1];
                var to = jointPlans[segment];
                var angles = new float[to.Length];
                for (int index = 0; index < angles.Length; index++) angles[index] = Mathf.Lerp(from[index], to[index], Mathf.SmoothStep(0, 1, fraction));
                adapter.SetJointAnglesDeg(angles);
                marker.position = adapter.ToolCenterPoint.position;
            }
            else marker.position = Vector3.Lerp(path[segment], path[segment + 1], fraction);
            if (elapsed - lastReport >= .7f)
            {
                lastReport = elapsed;
                yield return Report(job, "running", worldPoints, detail);
            }
            yield return null;
        }
        progress = 1;
        marker.position = jointReady ? adapter.ToolCenterPoint.position : worldPoints[worldPoints.Length - 1];
        stage = "COMPLETE";
        yield return Report(job, "completed", worldPoints, detail);
        Debug.Log($"A2A preview completed task={task} commands={accepted}/{expected} waypoints={pointsCount} kind={playbackKind}");
    }

    bool TryPlanWaypoint(UrdfRobotTwinAdapter adapter, Vector3 position, Quaternion rotation,
        float[] observedAngles, int waypoint, out float[] solution, out float error, out float angleError)
    {
        if (adapter.TryPlanToolPoseWorld(position, rotation, out solution, out error, out angleError, 192, .004f, 2f, 8f))
            return true;
        if (!adapter.TryGetJointAnglesDeg(out var precedingAngles)) return false;
        // 앞 경유점에서 IK가 실패해도 다음 자세가 불가능하다고 단정할 수 없습니다.
        // 오차 허용치와 관절 한계는 유지하고, 정해진 초기 자세로 제한 횟수만 재시도합니다.
        var seeds = new[] { observedAngles, new float[precedingAngles.Length] };
        try
        {
            for (int attempt = 0; attempt < seeds.Length; attempt++)
            {
                adapter.SetJointAnglesDeg(seeds[attempt]);
                var solved = adapter.TryPlanToolPoseWorld(position, rotation, out solution, out error, out angleError, 384, .004f, 2f, 4f);
                Debug.Log($"A2A preview IK retry task={task} waypoint={waypoint} attempt={attempt + 1} solved={solved} position_error={error} rotation_error={angleError}");
                if (solved) return true;
            }
            return false;
        }
        finally { adapter.SetJointAnglesDeg(precedingAngles); }
    }

    IEnumerator Report(Job job, string status, Vector3[] points, string message)
    {
        // 이번 작업의 보고 키와 승인 ID·경유점·진행률을 Supervisor에 전달합니다.
        var report = new Receipt {
            global_task_id = job.global_task_id, report_key = job.report_key,
            status = status, progress = progress, playback_kind = playbackKind,
            accepted_command_ids = accepted == expected ? job.expected_command_ids : Array.Empty<string>(),
            waypoints_world = points, message = message
        };
        using (var request = new UnityWebRequest(Api + "/report", "POST"))
        {
            request.uploadHandler = new UploadHandlerRaw(System.Text.Encoding.UTF8.GetBytes(JsonUtility.ToJson(report)));
            request.downloadHandler = new DownloadHandlerBuffer();
            request.SetRequestHeader("Content-Type", "application/json");
            request.timeout = 5;
            yield return request.SendWebRequest();
            if (request.result != UnityWebRequest.Result.Success)
                Debug.LogError("A2A preview report failed: " + request.responseCode + " " + request.downloadHandler.text);
        }
    }

    static Material Material(Color color)
    {
        var shader = Shader.Find("Sprites/Default") ?? Shader.Find("Unlit/Color") ?? Shader.Find("Standard");
        var material = new Material(shader) { color = color };
        return material;
    }

    void BuildVisuals(Vector3[] points)
    {
        visuals = new GameObject("A2A authoritative path");
        var line = visuals.AddComponent<LineRenderer>();
        line.useWorldSpace = true;
        line.positionCount = points.Length;
        line.SetPositions(points);
        line.startWidth = line.endWidth = .012f;
        line.material = Material(new Color(.1f, 1f, .85f));
        line.numCapVertices = 5;
        var roles = new [] { "1 SOURCE", "2 LIFT", "3 APPROACH", "4 DESTINATION" };
        for (int index = 0; index < points.Length; index++)
        {
            var point = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            point.name = "Waypoint " + index;
            point.transform.SetParent(visuals.transform);
            point.transform.position = points[index];
            point.transform.localScale = Vector3.one * .036f;
            Destroy(point.GetComponent<Collider>());
            point.GetComponent<Renderer>().material = Material(new Color(.1f, 1f, .85f));
            var label = new GameObject("Waypoint label");
            label.transform.SetParent(point.transform, false);
            label.transform.localPosition = new Vector3(0, 1.4f, 0);
            label.transform.localScale = Vector3.one * 2;
            var text = label.AddComponent<TextMesh>();
            text.text = index < roles.Length ? roles[index] : (index + 1).ToString();
            text.characterSize = .15f;
            text.fontSize = 64;
            text.anchor = TextAnchor.LowerCenter;
            text.color = Color.white;
        }
        var marker = GameObject.CreatePrimitive(PrimitiveType.Sphere);
        marker.name = "TCP preview";
        marker.transform.SetParent(visuals.transform);
        marker.transform.position = points[0];
        marker.transform.localScale = Vector3.one * .07f;
        Destroy(marker.GetComponent<Collider>());
        marker.GetComponent<Renderer>().material = Material(new Color(1f, .45f, .08f));
    }

    void FrameCamera(Vector3[] points)
    {
        var camera = Camera.main;
        if (camera == null) return;
        var bounds = new Bounds(points[0], Vector3.zero);
        foreach (var point in points) bounds.Encapsulate(point);
        var actual = TwinObjectBinding.FindByObjectId("skku/gp25");
        if (actual != null) bounds.Encapsulate(actual.TargetTransform.position + Vector3.up);
        var center = bounds.center + Vector3.up * .2f;
        camera.transform.position = center + new Vector3(1.5f, 1.6f, -2.4f);
        camera.transform.LookAt(center);
        camera.fieldOfView = 48;
        camera.clearFlags = CameraClearFlags.SolidColor;
        camera.backgroundColor = new Color(.06f, .09f, .12f);
        foreach (var renderer in FindObjectsByType<Renderer>(FindObjectsInactive.Exclude, FindObjectsSortMode.None))
        {
            var parent = renderer.transform;
            while (parent != null)
            {
                if (parent.name.IndexOf("fence", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    // 카메라 표시만 숨기며 장면의 형상과 충돌체는 그대로 둡니다.
                    renderer.gameObject.layer = 30;
                    break;
                }
                parent = parent.parent;
            }
        }
        camera.cullingMask &= ~(1 << 30);
        if (actual != null)
        {
            foreach (var renderer in actual.TargetTransform.GetComponentsInChildren<Renderer>()) renderer.gameObject.layer = 29;
            camera.cullingMask &= ~(1 << 29);
        }
        var targetRobot = TwinObjectBinding.FindByObjectId("skku/gp25_target");
        if (targetRobot != null)
            foreach (var renderer in targetRobot.TargetTransform.GetComponentsInChildren<Renderer>())
                foreach (var material in renderer.materials)
                {
                    var color = new Color(.2f, .65f, 1f, 1f);
                    if (material.HasProperty("_Color")) material.SetColor("_Color", color);
                    if (material.HasProperty("_BaseColor")) material.SetColor("_BaseColor", color);
                    if (material.HasProperty("_Mode")) material.SetFloat("_Mode", 0);
                    if (material.HasProperty("_ZWrite")) material.SetInt("_ZWrite", 1);
                    material.DisableKeyword("_ALPHABLEND_ON");
                    material.DisableKeyword("_ALPHAPREMULTIPLY_ON");
                    material.renderQueue = 2000;
                }
    }

    void LateUpdate()
    {
        if (visuals == null || Camera.main == null) return;
        foreach (var text in visuals.GetComponentsInChildren<TextMesh>())
            text.transform.rotation = Camera.main.transform.rotation;
    }

    void OnGUI()
    {
        var style = new GUIStyle(GUI.skin.label) {fontSize = 20};
        style.normal.textColor = Color.white;
        GUI.Box(new Rect(12, 12, Screen.width - 24, 117), "");
        GUI.Label(new Rect(26, 20, Screen.width - 52, 26), "A2A task: " + task, style);
        GUI.Label(new Rect(26, 47, Screen.width - 52, 26), $"{stage} | task commands {accepted}/{expected} | waypoints {pointsCount} | {progress:P0}", style);
        style.fontSize = 15;
        GUI.Label(new Rect(26, 80, Screen.width - 52, 40), detail, style);
    }

    [Serializable] sealed class Envelope { public Job job; public string session_id; }
    [Serializable] sealed class Job {
        public string global_task_id, path_id, object_id, source, destination, report_key;
        public string[] expected_command_ids;
        public int expected_waypoint_count;
    }
    [Serializable] sealed class Receipt {
        public string global_task_id, report_key, status, message, playback_kind;
        public float progress;
        public string[] accepted_command_ids;
        public Vector3[] waypoints_world;
    }
}
