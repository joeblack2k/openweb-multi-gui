import { WEBUI_BASE_URL } from '$lib/constants';

const BASE = `${WEBUI_BASE_URL}/api/omg/v1`;

const withAuth = (token: string) => ({
	Accept: 'application/json',
	'Content-Type': 'application/json',
	authorization: `Bearer ${token}`
});

const handle = async (res: Response) => {
	if (!res.ok) {
		let payload: any = null;
		try {
			payload = await res.json();
		} catch {
			payload = { detail: await res.text() };
		}
		throw payload;
	}
	return res.json();
};

export const getOMGVMs = async (token: string) => {
	const res = await fetch(`${BASE}/vms`, {
		method: 'GET',
		headers: withAuth(token)
	});
	return handle(res);
};

export const renameOMGVM = async (token: string, vmId: string, displayName: string) => {
	const res = await fetch(`${BASE}/vms/${encodeURIComponent(vmId)}`, {
		method: 'PATCH',
		headers: withAuth(token),
		body: JSON.stringify({ display_name: displayName })
	});
	return handle(res);
};

export const getOMGVMOpenAgents = async (token: string, vmId: string) => {
	const res = await fetch(`${BASE}/vms/${encodeURIComponent(vmId)}/agents`, {
		method: 'GET',
		headers: withAuth(token)
	});
	return handle(res);
};

export const createOMGAgent = async (
	token: string,
	vmId: string,
	body: { title: string; workdir_name: string; reuse_existing: boolean | null }
) => {
	const res = await fetch(`${BASE}/vms/${encodeURIComponent(vmId)}/agents`, {
		method: 'POST',
		headers: withAuth(token),
		body: JSON.stringify(body)
	});
	return handle(res);
};

export const sendOMGAgentMessage = async (token: string, agentId: string, content: string) => {
	const res = await fetch(`${BASE}/agents/${encodeURIComponent(agentId)}/messages`, {
		method: 'POST',
		headers: withAuth(token),
		body: JSON.stringify({ content })
	});
	return handle(res);
};

export const closeOMGAgent = async (token: string, agentId: string) => {
	const res = await fetch(`${BASE}/agents/${encodeURIComponent(agentId)}/close`, {
		method: 'POST',
		headers: withAuth(token)
	});
	return handle(res);
};

export const getOMGAgentEvents = async (token: string, agentId: string, cursor = 0) => {
	const url = new URL(`${BASE}/agents/${encodeURIComponent(agentId)}/events`);
	url.searchParams.set('cursor', String(cursor));

	const res = await fetch(url.toString(), {
		method: 'GET',
		headers: withAuth(token)
	});
	return handle(res);
};
