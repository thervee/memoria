import { defineRailway, github, project, service, volume } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "memoria";
export default defineRailway(() => {
  // Memoria's data (memories, accounts, keys): must stay attached at /data.
  const data = volume("memoria-volume", { region: "iad", sizeMB: 500 });
  const memoria = service("memoria", {
    source: github("thervee/memoria", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/health",
    healthcheckTimeout: 300,
    replicas: { "iad": 1 },
    deploy: {
      limitOverride: { containers: { cpu: 8, memoryBytes: 2000000000 } },
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 10,
    },
    volumeMounts: { "/data": data },
  });

  return project("mindful-freedom", {
    variables: { managed: false },
    resources: [memoria, data],
  });
});
