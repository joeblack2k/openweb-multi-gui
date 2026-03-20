<script lang="ts">
	import { onMount, getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import Folder from '$lib/components/common/Folder.svelte';
	import {
		closeOMGAgent,
		createOMGAgent,
		getOMGAgentEvents,
		getOMGVMOpenAgents,
		getOMGVMs,
		renameOMGVM,
		sendOMGAgentMessage
	} from '$lib/apis/omg';

	export let token: string;

	const i18n = getContext('i18n');

	type VM = {
		vm_id: string;
		display_name: string;
		vm_hostname?: string;
		is_online: boolean;
		open_agents: number;
		last_seen_at?: number;
	};

	type AgentSession = {
		agent_id: string;
		vm_id: string;
		title: string;
		status: string;
		workdir_path: string;
	};

	type AgentEvent = {
		id: number;
		seq?: number;
		event_type: string;
		payload: any;
		created_at: number;
	};

	let open = true;
	let loading = false;
	let vms: VM[] = [];
	let sessionsByVm: Record<string, AgentSession[]> = {};
	let selectedAgentId: string | null = null;
	let selectedAgentEvents: AgentEvent[] = [];
	let selectedAgentCursor = 0;
	let outgoingMessage = '';
	let pollHandle: ReturnType<typeof setInterval> | null = null;
	let eventsPollHandle: ReturnType<typeof setInterval> | null = null;
	let renameVmId: string | null = null;
	let renameInput = '';

	const loadVMs = async () => {
		if (!token) return;
		loading = true;
		try {
			const data = await getOMGVMs(token);
			vms = data ?? [];

			for (const vm of vms) {
				const sessions = await getOMGVMOpenAgents(token, vm.vm_id).catch(() => []);
				sessionsByVm[vm.vm_id] = sessions ?? [];
			}
		} catch (err: any) {
			console.error(err);
		} finally {
			loading = false;
		}
	};

	const startRename = (vm: VM) => {
		renameVmId = vm.vm_id;
		renameInput = vm.display_name;
	};

	const submitRename = async (vm: VM) => {
		const next = renameInput.trim();
		if (!next) {
			renameVmId = null;
			return;
		}
		try {
			await renameOMGVM(token, vm.vm_id, next);
			await loadVMs();
		} catch (err: any) {
			toast.error(err?.detail?.message ?? err?.detail ?? 'Rename failed');
		} finally {
			renameVmId = null;
		}
	};

	const createAgentFlow = async (vm: VM) => {
		const workdir = window.prompt('Workdir name', 'workspace');
		if (!workdir) return;

		const title = window.prompt('Agent title', `Agent ${workdir}`) ?? `Agent ${workdir}`;

		const runCreate = async (reuseExisting: boolean | null) => {
			return createOMGAgent(token, vm.vm_id, {
				title,
				workdir_name: workdir.trim(),
				reuse_existing: reuseExisting
			});
		};

		try {
			await runCreate(null);
			await loadVMs();
		} catch (err: any) {
			const code = err?.detail?.code;
			if (code === 'WORKDIR_EXISTS') {
				const shouldReuse = window.confirm(
					'Workdir bestaat al. Wil je die bestaande map hergebruiken?'
				);
				if (!shouldReuse) return;
				await runCreate(true);
				await loadVMs();
				return;
			}
			toast.error(err?.detail?.message ?? err?.detail ?? 'Agent create failed');
		}
	};

	const closeAgentFlow = async (session: AgentSession) => {
		try {
			await closeOMGAgent(token, session.agent_id);
			if (selectedAgentId === session.agent_id) {
				selectedAgentId = null;
				selectedAgentEvents = [];
				selectedAgentCursor = 0;
			}
			await loadVMs();
		} catch (err: any) {
			toast.error(err?.detail ?? 'Close failed');
		}
	};

	const pollSelectedAgentEvents = async () => {
		if (!selectedAgentId || !token) return;
		try {
			const res = await getOMGAgentEvents(token, selectedAgentId, selectedAgentCursor);
			const items: AgentEvent[] = res?.items ?? [];
			if (items.length > 0) {
				selectedAgentEvents = [...selectedAgentEvents, ...items];
				selectedAgentCursor = res?.next_cursor ?? selectedAgentCursor;
			}
		} catch (err) {
			console.error(err);
		}
	};

	const openAgent = async (session: AgentSession) => {
		selectedAgentId = session.agent_id;
		selectedAgentEvents = [];
		selectedAgentCursor = 0;
		await pollSelectedAgentEvents();
	};

	const sendMessage = async () => {
		const content = outgoingMessage.trim();
		if (!content || !selectedAgentId) return;
		try {
			await sendOMGAgentMessage(token, selectedAgentId, content);
			outgoingMessage = '';
			await pollSelectedAgentEvents();
		} catch (err: any) {
			toast.error(err?.detail ?? 'Send failed');
		}
	};

	onMount(() => {
		loadVMs();
		pollHandle = setInterval(loadVMs, 5000);
		eventsPollHandle = setInterval(pollSelectedAgentEvents, 2000);

		return () => {
			if (pollHandle) clearInterval(pollHandle);
			if (eventsPollHandle) clearInterval(eventsPollHandle);
		};
	});
</script>

<Folder id="sidebar-vm-agents" bind:open className="px-2 mt-0.5" name="VM Agents" chevron={false}>
	{#if loading && vms.length === 0}
		<div class="text-xs text-gray-500 px-3 py-2">Loading...</div>
	{/if}

	{#if !loading && vms.length === 0}
		<div class="text-xs text-gray-500 px-3 py-2">Geen VM agents gevonden.</div>
	{/if}

	{#each vms as vm (`vm-${vm.vm_id}`)}
		<div class="px-2 py-2 rounded-xl hover:bg-gray-100/80 dark:hover:bg-gray-900/80">
			<div class="flex items-center gap-2">
				<div
					class="size-2 rounded-full {vm.is_online
						? 'bg-green-500'
						: 'bg-gray-300 dark:bg-gray-700'}"
				></div>

				{#if renameVmId === vm.vm_id}
					<input
						class="bg-transparent border border-gray-300 dark:border-gray-700 rounded px-1.5 py-0.5 text-xs flex-1 min-w-0"
						bind:value={renameInput}
						on:keydown={(e) => {
							if (e.key === 'Enter') {
								submitRename(vm);
							}
							if (e.key === 'Escape') {
								renameVmId = null;
							}
						}}
						on:blur={() => submitRename(vm)}
					/>
				{:else}
					<button
						class="text-left flex-1 min-w-0 text-xs font-medium truncate"
						title="Rename VM header"
						on:click={() => startRename(vm)}
					>
						{vm.display_name}
					</button>
				{/if}

				<button
					class="text-[10px] px-2 py-1 rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-white dark:hover:bg-gray-800"
					on:click={() => createAgentFlow(vm)}
				>
					New Agent
				</button>
			</div>

			<div class="mt-1 text-[10px] text-gray-500">
				{vm.vm_hostname ?? vm.vm_id} · {vm.open_agents} open
			</div>

			<div class="mt-2 space-y-1">
				{#if (sessionsByVm[vm.vm_id] ?? []).length === 0}
					<div class="text-[11px] text-gray-500 px-1">No active sessions</div>
				{/if}

				{#each sessionsByVm[vm.vm_id] ?? [] as session (`agent-${session.agent_id}`)}
					<div
						class="rounded-lg border border-gray-100 dark:border-gray-800 px-2 py-1.5 {selectedAgentId === session.agent_id
							? 'bg-white dark:bg-gray-850'
							: ''}"
					>
						<div class="flex items-center gap-2">
							<button
								class="text-left text-xs flex-1 min-w-0 truncate"
								on:click={() => openAgent(session)}
								title={session.workdir_path}
							>
								{session.title}
							</button>
							<button
								class="text-[10px] px-1.5 py-0.5 rounded border border-gray-200 dark:border-gray-700"
								on:click={() => closeAgentFlow(session)}
							>
								Close
							</button>
						</div>
						<div class="text-[10px] text-gray-500 truncate">{session.workdir_path}</div>
					</div>
				{/each}
			</div>
		</div>
	{/each}

	{#if selectedAgentId}
		<div class="mt-2 mx-2 border border-gray-100 dark:border-gray-800 rounded-xl overflow-hidden">
			<div class="px-2 py-1 text-[11px] font-medium border-b border-gray-100 dark:border-gray-800">
				Agent Output
			</div>
			<div class="max-h-40 overflow-y-auto text-[11px] px-2 py-1.5 space-y-1">
				{#if selectedAgentEvents.length === 0}
					<div class="text-gray-500">Nog geen events.</div>
				{/if}
				{#each selectedAgentEvents as ev (`ev-${ev.id}`)}
					<div class="leading-snug">
						<div class="font-medium">{ev.event_type}</div>
						<div class="text-gray-500 break-words">{JSON.stringify(ev.payload)}</div>
					</div>
				{/each}
			</div>
			<div class="border-t border-gray-100 dark:border-gray-800 p-1.5 flex gap-1">
				<input
					class="flex-1 bg-transparent border border-gray-200 dark:border-gray-700 rounded px-2 py-1 text-xs"
					placeholder={$i18n?.t ? $i18n.t('Send a message') : 'Send a message'}
					bind:value={outgoingMessage}
					on:keydown={(e) => {
						if (e.key === 'Enter') sendMessage();
					}}
				/>
				<button
					class="text-xs px-2 py-1 rounded border border-gray-200 dark:border-gray-700 hover:bg-white dark:hover:bg-gray-800"
					on:click={sendMessage}
				>
					Send
				</button>
			</div>
		</div>
	{/if}
</Folder>
