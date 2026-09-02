import { Repo } from "./repo";

const r = new Repo();

export function runInstance(): void { r.save(); }

export function runQualified(): void { Repo.staticSave(); }
