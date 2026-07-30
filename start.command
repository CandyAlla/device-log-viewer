#!/bin/zsh
set -eu

viewer_dir="${0:A:h}"
helper_app="$viewer_dir/OpenDeviceLogViewerFolder.app"
launch_services_register="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
profile_path="${1:-$viewer_dir/profiles/default.json}"

if [[ $# -gt 1 ]]; then
  echo "用法：./start.command [Profile JSON 路径]"
  exit 2
fi
if [[ "$profile_path" != /* ]]; then
  profile_path="$PWD/$profile_path"
fi

tool_id="device-log-viewer"
tool_version="$(python3 "$viewer_dir/server.py" --version)"
profile_id="$(python3 "$viewer_dir/server.py" --profile "$profile_path" --print-profile-id)"
default_port="$(python3 "$viewer_dir/server.py" --profile "$profile_path" --print-port)"
selected_port=$default_port
last_port=$((default_port + 10))
if (( last_port > 65535 )); then
  last_port=65535
fi

if [[ -d "$helper_app" && -x "$launch_services_register" ]]; then
  "$launch_services_register" -f "$helper_app" >/dev/null 2>&1 || true
fi

listener_pid() {
  /usr/sbin/lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | /usr/bin/sed -n '1p'
}

listener_cwd() {
  /usr/sbin/lsof -a -p "$1" -d cwd -Fn 2>/dev/null | /usr/bin/sed -n 's/^n//p'
}

stop_viewer_pid() {
  local viewer_pid="$1"
  /bin/kill "$viewer_pid" 2>/dev/null || true
  for _attempt in {1..30}; do
    if ! /bin/kill -0 "$viewer_pid" 2>/dev/null; then
      return 0
    fi
    /bin/sleep 0.1
  done
  echo "无法停止旧服务。请关闭之前的终端窗口后再试。"
  return 1
}

for ((running_port=default_port; running_port<=last_port; running_port++)); do
  running_pid="$(listener_pid "$running_port")"
  if [[ -z "$running_pid" || "$(listener_cwd "$running_pid")" != "$viewer_dir" ]]; then
    continue
  fi
  running_url="http://127.0.0.1:$running_port"
  running_status="$(/usr/bin/curl --silent --max-time 2 "$running_url/api/status" 2>/dev/null || true)"
  if [[ "$running_status" == *"\"toolId\": \"$tool_id\""* \
    && "$running_status" == *"\"version\": \"$tool_version\""* \
    && "$running_status" == *"\"profileId\": \"$profile_id\""* ]]; then
    echo "Device Log Viewer 已运行，正在打开：$running_url"
    /usr/bin/open "$running_url"
    exit 0
  fi
  echo "检测到同目录的其他版本或 Profile，正在停止旧服务…"
  stop_viewer_pid "$running_pid"
done

existing_pid="$(listener_pid "$default_port")"
if [[ -n "$existing_pid" ]]; then
  existing_cwd="$(listener_cwd "$existing_pid")"

  if [[ "$existing_cwd" == "$viewer_dir" ]]; then
    echo "检测到同目录的其他版本或 Profile，正在重新启动…"
    stop_viewer_pid "$existing_pid"
  else
    for ((candidate_port=default_port+1; candidate_port<=last_port; candidate_port++)); do
      if [[ -z "$(listener_pid "$candidate_port")" ]]; then
        selected_port=$candidate_port
        break
      fi
    done
    if [[ "$selected_port" == "$default_port" ]]; then
      echo "端口 $default_port–$last_port 均被占用，请关闭占用端口的程序后再试。"
      exit 1
    fi
    echo "端口 $default_port 被其他程序占用，将改用端口 $selected_port。"
  fi
fi

cd "$viewer_dir"
exec python3 server.py --profile "$profile_path" --port "$selected_port"
