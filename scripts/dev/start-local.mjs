#!/usr/bin/env node

import { randomBytes } from "node:crypto";
import { spawn, execFile } from "node:child_process";
import { promisify } from "node:util";
import { createServer } from "node:net";
import {
  chmod,
  lstat,
  mkdir,
  open,
  readFile,
  rename,
  unlink,
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import readline from "node:readline";

const execFileAsync = promisify(execFile);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const runtimeDir = path.join(root, ".local-run");
const envPath = path.join(runtimeDir, "local.env");
const lockPath = path.join(runtimeDir, "launcher.json");
const composeFile = path.join(root, "deploy/docker-compose.dev.yml");
const projectName = "sro-robot-local";
const python = path.join(root, ".venv/bin/python");
const launcherIdentity = "scripts/dev/start-local.mjs";
const children = new Map();
let currentCommand = null;
let stopping = false;
let lockNonce = null;
let composeStarted = false;
let shutdownPromise = null;
let exitCode = 0;
let requestedPort = Number(process.env.SRO_LOCAL_PORT || 8000);

function fail(message) {
  throw new Error(message);
}

function composeArgs(envFile, args, quiet = false) {
  return [
    "compose",
    ...(quiet ? ["--progress", "quiet"] : []),
    "--project-name", projectName,
    "--project-directory", root,
    "--env-file", envFile,
    "-f", composeFile,
    ...args,
  ];
}

function parseEnv(contents) {
  const values = {};
  for (const line of contents.split(/\r?\n/)) {
    const match = line.match(/^([A-Z][A-Z0-9_]*)=(.*)$/);
    if (match) values[match[1]] = match[2];
  }
  return values;
}

function renderEnv(values) {
  return Object.entries(values).map(([key, value]) => `${key}=${value}`).join("\n") + "\n";
}

function validLocalConfig(values) {
  const allowedKeys = new Set([
    "SRO_LOCAL_CONFIG_VERSION", "APP_DEBUG", "APP_SECRET_KEY", "POSTGRES_USER",
    "POSTGRES_DB", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "DATABASE_URL",
    "REDIS_URL", "OCR_TASK_LEASES_ENABLED",
  ]);
  return Object.keys(values).length === allowedKeys.size
    && Object.keys(values).every((key) => allowedKeys.has(key))
    && values.SRO_LOCAL_CONFIG_VERSION === "1"
    && values.APP_DEBUG === "1"
    && values.POSTGRES_USER === "sro"
    && values.POSTGRES_DB === "sro"
    && /^[a-f0-9]{48}$/.test(values.POSTGRES_PASSWORD || "")
    && /^[a-f0-9]{64}$/.test(values.REDIS_PASSWORD || "")
    && /^[a-f0-9]{64}$/.test(values.APP_SECRET_KEY || "")
    && values.DATABASE_URL === `postgresql+psycopg://sro:${values.POSTGRES_PASSWORD}@127.0.0.1:55432/sro`
    && values.REDIS_URL === `redis://:${values.REDIS_PASSWORD}@127.0.0.1:56379/0`
    && values.OCR_TASK_LEASES_ENABLED === "1";
}

async function ensurePrivateRuntimeDir() {
  await mkdir(runtimeDir, { recursive: true, mode: 0o700 });
  const metadata = await lstat(runtimeDir);
  if (!metadata.isDirectory() || metadata.isSymbolicLink() || metadata.uid !== process.getuid?.()) {
    fail(".local-run must be a real directory owned by the current user");
  }
  await chmod(runtimeDir, 0o700);
}

async function loadOrCreateLocalConfig() {
  await ensurePrivateRuntimeDir();
  try {
    const metadata = await lstat(envPath);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.uid !== process.getuid?.()) {
      fail(".local-run/local.env must be a regular file owned by the current user");
    }
    if ((metadata.mode & 0o077) !== 0) fail(".local-run/local.env permissions must be 0600");
    const contents = await readFile(envPath, "utf8");
    const values = parseEnv(contents);
    if (!validLocalConfig(values)) {
      fail(".local-run/local.env is not a valid generated local configuration; move it aside to regenerate it");
    }
    return values;
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }

  const postgresPassword = randomBytes(24).toString("hex");
  const redisPassword = randomBytes(32).toString("hex");
  const values = {
    SRO_LOCAL_CONFIG_VERSION: "1",
    APP_DEBUG: "1",
    APP_SECRET_KEY: randomBytes(32).toString("hex"),
    POSTGRES_USER: "sro",
    POSTGRES_DB: "sro",
    POSTGRES_PASSWORD: postgresPassword,
    REDIS_PASSWORD: redisPassword,
    DATABASE_URL: `postgresql+psycopg://sro:${postgresPassword}@127.0.0.1:55432/sro`,
    REDIS_URL: `redis://:${redisPassword}@127.0.0.1:56379/0`,
    OCR_TASK_LEASES_ENABLED: "1",
  };
  const temporaryPath = `${envPath}.${process.pid}.tmp`;
  const handle = await open(temporaryPath, "wx", 0o600);
  try {
    await handle.writeFile(renderEnv(values), "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
  await rename(temporaryPath, envPath);
  await chmod(envPath, 0o600);
  return values;
}

function localChildEnv(values, { migration = false } = {}) {
  const env = { ...process.env };
  for (const key of [
    "DATABASE_URL", "REDIS_URL", "MIGRATION_DATABASE_URL", "SRO_LOGIN", "SRO_PASSWORD",
    "APP_PUBLIC_ORIGIN", "SRO_UPLOAD_DIR", "SRO_PROFILE_DIR", "SRO_OUT_DIR",
  ]) {
    delete env[key];
  }
  Object.assign(env, values, { SRO_ROBOT_ENV_FILE: envPath, PYTHONUNBUFFERED: "1" });
  if (migration) env.MIGRATION_DATABASE_URL = values.DATABASE_URL;
  else delete env.MIGRATION_DATABASE_URL;
  return env;
}

async function existingLauncher(pid) {
  if (!Number.isInteger(pid) || pid < 2) return false;
  try {
    process.kill(pid, 0);
    const { stdout } = await execFileAsync("ps", ["-p", String(pid), "-o", "command="], { timeout: 2000 });
    if (!stdout.includes(launcherIdentity)) return false;
    const cwd = await execFileAsync("lsof", ["-a", "-p", String(pid), "-d", "cwd", "-Fn"], { timeout: 2000 });
    return cwd.stdout.split(/\r?\n/).includes(`n${root}`);
  } catch {
    return false;
  }
}

async function acquireLock() {
  try {
    const handle = await open(lockPath, "wx", 0o600);
    lockNonce = randomBytes(16).toString("hex");
    await handle.writeFile(JSON.stringify({ pid: process.pid, port: requestedPort, nonce: lockNonce }));
    await handle.close();
    return true;
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
    let prior = {};
    try { prior = JSON.parse(await readFile(lockPath, "utf8")); } catch {}
    if (await existingLauncher(prior.pid)) {
      console.log(`SRO Robot уже запущен (PID ${prior.pid}). Адрес: http://127.0.0.1:${prior.port || 8000}`);
      return false;
    }
    await unlink(lockPath).catch((unlinkError) => {
      if (unlinkError.code !== "ENOENT") throw unlinkError;
    });
    return acquireLock();
  }
}

async function assertPortAvailable(port) {
  await new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", () => reject(new Error(`Порт ${port} уже занят. Остановите локальный сервис на этом порту или задайте SRO_LOCAL_PORT.`)));
    server.listen(port, "127.0.0.1", () => server.close((error) => error ? reject(error) : resolve()));
  });
}

function prefixOutput(stream, label) {
  const lines = readline.createInterface({ input: stream });
  lines.on("line", (line) => console.log(`[${label}] ${line}`));
  return lines;
}

function spawnManaged(label, command, args, env, { detached = true } = {}) {
  const child = spawn(command, args, {
    cwd: root,
    env,
    stdio: ["ignore", "pipe", "pipe"],
    detached: detached && process.platform !== "win32",
  });
  children.set(child, label);
  prefixOutput(child.stdout, label);
  prefixOutput(child.stderr, label);
  child.once("error", (error) => {
    if (!stopping) {
      console.error(`${label}: не удалось запустить процесс (${error.code || error.name})`);
      exitCode = 1;
      void shutdown();
    }
  });
  child.once("exit", (code, signal) => {
    children.delete(child);
    if (!stopping) {
      console.error(`${label}: процесс завершился неожиданно (${signal || `код ${code}`})`);
      exitCode = 1;
      void shutdown();
    }
  });
  return child;
}

function signalProcess(child, signal) {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  try {
    if (process.platform !== "win32" && child.spawnargs && child.pid) process.kill(-child.pid, signal);
    else child.kill(signal);
  } catch (error) {
    if (error.code !== "ESRCH") console.error(`Не удалось остановить дочерний процесс: ${error.code || error.name}`);
  }
}

async function stopChildren() {
  const active = [...children.keys()];
  if (active.length === 0) return;
  for (const child of active) signalProcess(child, "SIGTERM");
  let graceTimer;
  await Promise.race([
    Promise.all(active.map((child) => new Promise((resolve) => {
      if (child.exitCode !== null || child.signalCode !== null) return resolve();
      child.once("exit", resolve);
      child.once("error", resolve);
    }))),
    new Promise((resolve) => { graceTimer = setTimeout(resolve, 12000); }),
  ]);
  clearTimeout(graceTimer);
  for (const child of children.keys()) signalProcess(child, "SIGKILL");
  await new Promise((resolve) => setTimeout(resolve, 250));
}

async function runCommand(label, command, args, env, { timeoutMs = 120000 } = {}) {
  console.log(`${label}…`);
  await new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: root, env, stdio: "inherit" });
    currentCommand = child;
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`${label}: превышено время ожидания`));
    }, timeoutMs);
    child.once("error", (error) => {
      clearTimeout(timer);
      currentCommand = null;
      reject(new Error(`${label}: ${error.code || error.name}`));
    });
    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      currentCommand = null;
      if (stopping) return reject(new Error("Запуск остановлен пользователем"));
      if (code !== 0) return reject(new Error(`${label}: команда завершилась (${signal || `код ${code}`})`));
      resolve();
    });
  });
}

async function waitUntilReady(port, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  while (!stopping && Date.now() < deadline) {
    if ([...children.keys()].some((child) => child.exitCode !== null || child.signalCode !== null)) {
      fail("веб-приложение или worker завершился во время запуска");
    }
    try {
      const response = await fetch(`http://127.0.0.1:${port}/readyz`, { signal: AbortSignal.timeout(1500) });
      if (response.ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 750));
  }
  if (stopping) fail("запуск остановлен пользователем");
  fail("веб-приложение не стало готово за 120 секунд; проверьте сообщения выше");
}

async function cleanupLock() {
  if (!lockNonce) return;
  try {
    const current = JSON.parse(await readFile(lockPath, "utf8"));
    if (current.pid === process.pid && current.nonce === lockNonce) await unlink(lockPath);
  } catch (error) {
    if (error.code !== "ENOENT") console.error("Не удалось очистить локальный lock-файл");
  }
}

async function shutdown() {
  if (shutdownPromise) return shutdownPromise;
  stopping = true;
  shutdownPromise = (async () => {
    if (currentCommand && currentCommand.exitCode === null) currentCommand.kill("SIGTERM");
    await stopChildren();
    if (composeStarted) {
      console.log("Останавливаю локальные PostgreSQL и Redis (данные сохраняются)…");
      try {
        const values = parseEnv(await readFile(envPath, "utf8"));
        await runComposeDown(values);
      } catch (error) {
        console.error(`Не удалось остановить локальные контейнеры: ${error.message}`);
        exitCode = 1;
      }
    }
    await cleanupLock();
  })();
  return shutdownPromise;
}

async function runComposeDown(values) {
  await new Promise((resolve) => {
    const child = spawn("docker", composeArgs(envPath, ["down", "--remove-orphans"], true), {
      cwd: root,
      env: localChildEnv(values),
      stdio: "inherit",
      detached: process.platform !== "win32",
    });
    child.once("error", (error) => {
      console.error(`docker compose down: ${error.code || error.name}`);
      resolve();
    });
    child.once("exit", (code) => {
      if (code !== 0) {
        console.error(`docker compose down завершился с кодом ${code}`);
        exitCode = 1;
      }
      resolve();
    });
  });
}

async function main() {
  if (!Number.isInteger(requestedPort) || requestedPort < 1024 || requestedPort > 65535) {
    fail("SRO_LOCAL_PORT должен быть числом от 1024 до 65535");
  }
  await ensurePrivateRuntimeDir();
  if (!(await acquireLock())) return;
  process.on("SIGINT", () => {
    if (stopping) return;
    console.log("\nПолучен Ctrl+C; корректно останавливаю локальные сервисы…");
    exitCode ||= 130;
    void shutdown();
  });
  process.on("SIGTERM", () => {
    if (stopping) return;
    console.log("\nПолучен SIGTERM; корректно останавливаю локальные сервисы…");
    exitCode ||= 143;
    void shutdown();
  });
  process.on("SIGHUP", () => {
    if (stopping) return;
    console.log("\nТерминал закрывается; корректно останавливаю локальные сервисы…");
    exitCode ||= 129;
    void shutdown();
  });

  try {
    await assertPortAvailable(requestedPort);
    const values = await loadOrCreateLocalConfig();
    const env = localChildEnv(values);
    try {
      await execFileAsync(python, ["--version"], { cwd: root, timeout: 3000 });
    } catch {
      fail("Не найден рабочий .venv/bin/python. Создайте виртуальное окружение и установите requirements.txt.");
    }
    for (const [binary, args] of [["tesseract", ["--version"]], ["pdftoppm", ["-v"]]]) {
      try { await execFileAsync(binary, args, { timeout: 3000 }); }
      catch { fail(`Не найдена локальная OCR-утилита ${binary}; установите системные Tesseract и Poppler.`); }
    }
    console.log("Запускаю отдельные локальные PostgreSQL и Redis; корневой .env и внешние базы не используются.");
    composeStarted = true;
    await runCommand("PostgreSQL и Redis", "docker", composeArgs(envPath, ["up", "-d", "--wait", "--wait-timeout", "120", "db", "redis"], true), env, { timeoutMs: 140000 });
    await runCommand("Применение локальных миграций", python, ["-m", "app.cli", "init-db"], localChildEnv(values, { migration: true }));

    const workerCount = Number(process.env.SRO_LOCAL_OCR_WORKERS || 2);
    if (!Number.isInteger(workerCount) || workerCount < 1 || workerCount > 4) {
      fail("SRO_LOCAL_OCR_WORKERS должен быть числом от 1 до 4");
    }
    spawnManaged("OCR worker", python, ["scripts/dev/run_worker.py", "ocr", "--workers", String(workerCount)], env);
    spawnManaged("SRO preview worker", python, ["scripts/dev/run_worker.py", "sro_preview"], env);
    spawnManaged("OCR outbox dispatcher", python, ["-m", "app.cli", "outbox-dispatch", "--apply", "--watch"], env);
    spawnManaged("Web", python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(requestedPort), "--reload", "--no-access-log"], env);
    await waitUntilReady(requestedPort);
    console.log(`SRO Robot готов: http://127.0.0.1:${requestedPort}`);
    console.log(`Локальные сервисы: PostgreSQL, Redis, OCR (${workerCount} worker-а), SRO preview worker и веб-приложение.`);
    console.log("Для остановки нажмите Ctrl+C. Данные локальной БД сохраняются.");
    await new Promise((resolve) => {
      const check = setInterval(() => {
        if (stopping || children.size === 0) {
          clearInterval(check);
          resolve();
        }
      }, 250);
    });
  } catch (error) {
    if (!stopping) {
      console.error(`Не удалось запустить SRO Robot: ${error.message}`);
      exitCode = 1;
    }
    await shutdown();
  } finally {
    await shutdown();
    await cleanupLock();
  }
}

main().catch(async (error) => {
  console.error(`Ошибка запуска: ${error.message}`);
  exitCode = 1;
  await shutdown();
}).finally(() => {
  process.exitCode = exitCode;
});
