frappe.pages["venta-rapida"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Facturación Rápida",
		single_column: true,
	});

	new CimaSellerPortal(page, wrapper);
};

class CimaSellerPortal {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = $(wrapper);
		this.state = {
			activeTab: "facturar",
			bootstrap: {
				price_lists: [],
				warehouses: [],
				sales_channels: [],
				sales_persons: [],
				seller_options: [],
				default_price_list: "",
				default_warehouse: "",
				default_sales_channel: "Detal",
				default_sales_person: "",
				auto_discount_enabled: true,
				auto_discount_tiers: [],
			},
			priceList: "",
			warehouse: "",
			salesChannel: "Detal",
			commissionOption: "",
			selectedSellerUser: "",
			selectedSalesPerson: "",
			dueDate: "",
			selectedCustomer: null,
			manualDiscountEnabled: false,
			manualDiscountPct: 0,
			cart: [],
			inventoryItems: [],
			lastInvoiceResult: null,
		};
		this._pricingSeq = 0;
		this._pricingTimer = null;

		this.renderShell();
		this.bindEvents();
		this.syncAdminVisibility();
		this.loadBootstrap();
	}

	isAdmin() {
		return Boolean(
			(this.state.bootstrap && this.state.bootstrap.is_admin) ||
			(frappe.user && frappe.user.has_role && frappe.user.has_role("System Manager"))
		);
	}

	syncAdminVisibility() {
		this.wrapper.find("[data-admin-only='1']").toggle(this.isAdmin());
	}

	renderShell() {
		const html = `
			<div class="cima-seller-shell">
				<div class="cima-hero-bar">
					<div class="cima-hero-brand">
						<img src="/assets/cima_ve/images/cima-logo-blanco.png" alt="CIMA" class="cima-hero-logo" onerror="this.style.display='none'" />
						<div class="cima-hero-title">
							<h2>Facturación Rápida</h2>
							<p>Emite notas de entrega y consulta inventario y precios.</p>
						</div>
					</div>
					<div class="cima-hero-actions">
						<button type="button" class="cima-btn-start-invoice" data-action="start-invoice">
							+ Nota de Entrega
						</button>
						<button type="button" class="cima-btn-cashea" data-action="show-cashea-qr">
							QR Cashea
						</button>
						<button type="button" class="cima-btn-inventory" data-action="tab-inventory">
							Ver Inventario y Precios
						</button>
						<button type="button" class="cima-btn-inventory" data-action="open-delivery-notes">
							Notas de Entrega
						</button>
						<button type="button" class="cima-btn-inventory" data-action="open-client-consumption" data-admin-only="1" style="display: none;">
							Clientes y Consumo
						</button>
						<button type="button" class="cima-btn-inventory" data-action="open-metrics" data-admin-only="1" style="display: none;">
							Métricas de Ventas
						</button>
						<button type="button" class="cima-btn-inventory" data-action="open-product-loader" data-admin-only="1" style="display: none;">
							Cargar Productos / Lotes
						</button>
					</div>
				</div>

				<div id="cima-result-area"></div>

				<div id="cima-view-facturar">
					<div class="cima-grid-invoice">
						<div class="cima-card">
							<div class="cima-card-title">
								<span>1. Cliente y Productos</span>
								<button type="button" class="btn btn-xs btn-default" data-action="new-customer">
									+ Nuevo Cliente
								</button>
							</div>

							<div class="cima-search-row">
								<input type="text" id="cima-customer-search" class="form-control cima-search-input"
									placeholder="Buscar cliente por nombre o Cédula/RIF..." />
								<select id="cima-customer-select" class="form-control" style="max-width: 240px;">
									<option value="">-- Seleccionar cliente --</option>
								</select>
							</div>
							<div class="cima-search-row" style="margin-top:-4px;margin-bottom:8px;">
								<input type="email" id="cima-customer-email" class="form-control input-sm"
									placeholder="Correo electrónico del cliente (obligatorio para Nota de Entrega)" required />
							</div>

							<div class="cima-search-row">
								<select id="cima-price-list" class="form-control" style="max-width: 220px;"></select>
								<select id="cima-warehouse" class="form-control" style="max-width: 220px;"></select>
							</div>

							<div class="cima-search-row">
								<input type="text" id="cima-invoice-item-search" class="form-control cima-search-input"
									placeholder="Buscar producto por código, nombre o categoría..." />
							</div>

							<div id="cima-invoice-item-results" class="cima-product-list">
								<div class="text-muted text-center p-4">Cargando productos...</div>
							</div>
						</div>

						<div class="cima-card">
							<div class="cima-card-title">
								<span>2. Resumen de Nota de Entrega</span>
								<button type="button" class="btn btn-xs btn-default" data-action="clear-cart">
									Limpiar
								</button>
							</div>
							<div id="cima-logged-seller-badge" class="text-muted small mb-2"></div>
							<div id="cima-admin-seller-box" class="form-group mt-1 mb-2" data-admin-only="1" style="display: none;">
								<label class="control-label" for="cima-seller-select">Vendedor asignado (Caja / Admin)</label>
								<select id="cima-seller-select" class="form-control"></select>
							</div>

							<div id="cima-cart-items">
								<div class="text-muted text-center p-4">
									Agrega productos desde la izquierda para comenzar.
								</div>
							</div>

							<div class="form-group mt-3">
								<label class="control-label">Canal de Venta / Comisión</label>
								<select id="cima-sales-channel" class="form-control"></select>
							</div>

							<div class="form-group mt-2">
								<label class="control-label">Fecha de Vencimiento <span style="color:#dc2626;">*</span></label>
								<input type="date" id="cima-due-date" class="form-control" required />
							</div>

							<div class="form-group mt-2" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:8px 10px;">
								<label class="d-flex align-items-center mb-0" style="gap:8px;cursor:pointer;font-weight:600;">
									<input type="checkbox" id="cima-enable-discount" />
									<span>Habilitar descuento (%)</span>
								</label>
								<div id="cima-manual-discount-box" style="display:none;margin-top:8px;">
									<div class="input-group input-group-sm">
										<input type="number" id="cima-discount-pct" class="form-control" min="0" max="100" step="0.01" placeholder="Ej: 10" value="0" />
										<span class="input-group-addon" style="padding:4px 10px;background:#e2e8f0;border:1px solid #cbd5e1;border-left:0;">%</span>
									</div>
								</div>
							</div>

							<div class="cima-summary-box" id="cima-tax-summary">
								<div class="cima-summary-line"><span>Subtotal:</span><strong>0.00</strong></div>
								<div class="cima-summary-total"><span>TOTAL:</span><span>0.00</span></div>
							</div>

							<button type="button" id="cima-btn-emit" class="cima-btn-emit" disabled>
								EMITIR NOTA DE ENTREGA
							</button>
						</div>
					</div>
				</div>

				<div id="cima-view-inventario" style="display: none;">
					<div class="cima-card">
						<div class="cima-card-title">
							<span>Inventario y Precios</span>
							<button type="button" class="btn btn-sm btn-primary" data-action="start-invoice">
								Ir a Facturar
							</button>
						</div>
						<div class="cima-search-row">
							<input type="text" id="cima-inv-search" class="form-control cima-search-input"
								placeholder="Buscar por código, nombre o categoría..." />
							<select id="cima-inv-price-list" class="form-control" style="max-width: 220px;"></select>
							<select id="cima-inv-warehouse" class="form-control" style="max-width: 220px;"></select>
						</div>
						<div class="table-responsive">
							<table class="table table-bordered table-hover">
								<thead>
									<tr>
										<th>Código</th>
										<th>Producto</th>
										<th>Categoría</th>
										<th class="text-right">Stock</th>
										<th class="text-right">Precio</th>
										<th class="text-center">Acción</th>
									</tr>
								</thead>
								<tbody id="cima-inv-table-body">
									<tr><td colspan="6" class="text-center text-muted">Cargando inventario...</td></tr>
								</tbody>
							</table>
						</div>
					</div>
				</div>
			</div>
		`;

		this.wrapper.find(".layout-main-section").html(html);
	}

	showCasheaQrModal() {
		const d = new frappe.ui.Dialog({
			title: "Pago con Cashea — Código QR Oficial",
			fields: [
				{
					fieldtype: "HTML",
					options: `
						<div class="cima-cashea-qr-card" style="margin-top:0;">
							<div style="font-weight:700;font-size:15px;color:#1c2532;">Cimaelectronic ve, C.A - Caja 1</div>
							<div style="font-size:12px;color:#5c6b73;margin-bottom:6px;">RIF: J501934070</div>
							<img src="/assets/cima_ve/images/cashea-qr.png" alt="Código QR Cashea GOHAQN" />
							<div style="font-size:12px;color:#5c6b73;margin-top:6px;">Código de comercio Cashea</div>
							<div class="cima-cashea-code">GOHAQN</div>
							<div style="font-size:12px;color:#475569;margin-top:10px;">
								El cliente escanea este QR desde la app de Cashea o ingresa el código <strong>GOHAQN</strong>.
							</div>
						</div>
					`,
				},
			],
			primary_action_label: "Cerrar",
			primary_action: () => d.hide(),
		});
		d.show();
	}

	bindEvents() {
		const root = this.wrapper;

		root.on("click", "[data-action='start-invoice']", () => {
			this.switchTab("facturar");
			root.find("#cima-invoice-item-search").focus();
		});

		root.on("click", "[data-action='tab-inventory']", () => {
			this.switchTab("inventario");
			this.loadInventory(root.find("#cima-inv-search").val() || "");
		});

		root.on("click", "[data-action='open-delivery-notes']", () => {
			frappe.set_route("notas-entrega");
		});

		root.on("click", "[data-action='open-client-consumption']", () => {
			if (!this.isAdmin()) return;
			frappe.set_route("clientes-consumo");
		});

		root.on("click", "[data-action='open-metrics']", () => {
			if (!this.isAdmin()) return;
			frappe.set_route("metricas-ventas");
		});

		root.on("click", "[data-action='show-cashea-qr']", () => {
			this.showCasheaQrModal();
		});

		root.on("click", "[data-action='open-product-loader']", () => {
			if (!this.isAdmin()) return;
			frappe.set_route("carga-productos");
		});

		root.on("click", "[data-action='clear-cart']", () => {
			clearTimeout(this._pricingTimer);
			this._pricingSeq += 1;
			this.state.cart = [];
			this.state.manualDiscountEnabled = false;
			this.state.manualDiscountPct = 0;
			root.find("#cima-enable-discount").prop("checked", false);
			root.find("#cima-discount-pct").val("0");
			root.find("#cima-manual-discount-box").hide();
			this.state.lastInvoiceResult = null;
			this.renderResultBanner();
			this.renderCart();
		});

		root.on("click", "[data-action='new-customer']", () => {
			this.openNewCustomerDialog();
		});

		let itemSearchTimer = null;
		root.on("input", "#cima-invoice-item-search", (e) => {
			clearTimeout(itemSearchTimer);
			itemSearchTimer = setTimeout(() => {
				this.loadInventory($(e.target).val());
			}, 220);
		});

		let invSearchTimer = null;
		root.on("input", "#cima-inv-search", (e) => {
			clearTimeout(invSearchTimer);
			invSearchTimer = setTimeout(() => {
				this.loadInventory($(e.target).val());
			}, 220);
		});

		let custSearchTimer = null;
		root.on("input", "#cima-customer-search", (e) => {
			clearTimeout(custSearchTimer);
			custSearchTimer = setTimeout(() => {
				this.loadCustomers($(e.target).val());
			}, 220);
		});

		root.on("change", "#cima-customer-select", (e) => {
			this.state.selectedCustomer = $(e.target).val() || null;
			const optEmail = $(e.target).find("option:selected").attr("data-email") || "";
			this.wrapper.find("#cima-customer-email").val(optEmail);
			this.renderCart();
			this.refreshCartPricing();
		});

		root.on("change", "#cima-price-list, #cima-inv-price-list", (e) => {
			this.state.priceList = $(e.target).val();
			root.find("#cima-price-list").val(this.state.priceList);
			root.find("#cima-inv-price-list").val(this.state.priceList);
			this.loadInventory(
				this.state.activeTab === "facturar"
					? root.find("#cima-invoice-item-search").val()
					: root.find("#cima-inv-search").val()
			);
			this.refreshCartPricing();
		});

		root.on("change", "#cima-warehouse, #cima-inv-warehouse", (e) => {
			this.state.warehouse = $(e.target).val();
			root.find("#cima-warehouse").val(this.state.warehouse);
			root.find("#cima-inv-warehouse").val(this.state.warehouse);
			this.loadInventory(
				this.state.activeTab === "facturar"
					? root.find("#cima-invoice-item-search").val()
					: root.find("#cima-inv-search").val()
			);
			this.refreshCartPricing();
		});

		root.on("change", "#cima-seller-select", (e) => {
			const sel = $(e.target);
			const userVal = (sel.val() || "").trim();
			const opt = sel.find("option:selected");
			const spVal = (opt.attr("data-sales-person") || "").trim();
			const fullVal = (opt.attr("data-fullname") || userVal).trim();
			this.state.selectedSellerUser = userVal;
			this.state.selectedSalesPerson = spVal;
			this.wrapper.find("#cima-logged-seller-badge").html(
				`Vendedor asignado: <strong>${frappe.utils.escape_html(fullVal)}</strong>`
			);
		});

		root.on("change", "#cima-sales-channel", (e) => {
			const selected = $(e.target).val() || "";
			this.state.commissionOption = selected;
			const rules = this.state.bootstrap?.commission_rules || [];
			const matched = rules.find((r) => r.label === selected || r.channel === selected);
			this.state.salesChannel = matched ? matched.channel : selected;
		});

		root.on("change input", "#cima-due-date", (e) => {
			this.state.dueDate = ($(e.target).val() || "").trim();
			this.wrapper.find("#cima-btn-emit").prop(
				"disabled",
				!this.state.selectedCustomer || !this.state.cart.length || !this.state.dueDate
			);
		});

		root.on("change", "#cima-enable-discount", (e) => {
			const enabled = $(e.target).is(":checked");
			this.state.manualDiscountEnabled = enabled;
			root.find("#cima-manual-discount-box").toggle(enabled);
			if (enabled) {
				setTimeout(() => root.find("#cima-discount-pct").focus().select(), 50);
			}
			this.updateSummaryFromCart();
		});

		root.on("input change", "#cima-discount-pct", (e) => {
			let val = parseFloat($(e.target).val());
			if (isNaN(val) || val < 0) val = 0;
			if (val > 100) val = 100;
			this.state.manualDiscountPct = val;
			this.updateSummaryFromCart();
		});

		root.on("click", "[data-add-item]", (e) => {
			const code = $(e.currentTarget).attr("data-add-item");
			const item = this.state.inventoryItems.find((r) => r.item_code === code);
			if (item) {
				this.addItemToCart(item);
				this.switchTab("facturar");
			}
		});

		root.on("click", "[data-add-custom-price]", (e) => {
			const code = $(e.currentTarget).attr("data-add-custom-price");
			const item = this.state.inventoryItems.find((r) => r.item_code === code);
			if (item) {
				this.addItemToCart(item);
				this.switchTab("facturar");
				this.openCustomPriceDialog(code);
			}
		});

		root.on("click", "[data-edit-price]", (e) => {
			const code = $(e.currentTarget).attr("data-edit-price");
			this.openCustomPriceDialog(code);
		});

		root.on("click", "[data-qty-delta]", (e) => {
			const code = $(e.currentTarget).attr("data-item-code");
			const delta = parseFloat($(e.currentTarget).attr("data-qty-delta"));
			this.updateCartQty(code, delta);
		});

		root.on("focus", "[data-qty-input]", (e) => {
			e.target.select();
		});

		root.on("keydown", "[data-qty-input]", (e) => {
			if (e.key === "Enter") {
				e.preventDefault();
				$(e.target).blur();
			}
		});

		root.on("input", "[data-qty-input]", (e) => {
			const code = $(e.currentTarget).attr("data-qty-input");
			const val = parseFloat($(e.currentTarget).val());
			if (!isNaN(val) && val > 0) {
				this.setCartQty(code, val, { skipRender: true, debouncePricing: true });
			}
		});

		root.on("change", "[data-qty-input]", (e) => {
			const code = $(e.currentTarget).attr("data-qty-input");
			let val = parseFloat($(e.currentTarget).val());
			if (isNaN(val) || val <= 0) {
				val = 1;
				$(e.currentTarget).val(1);
			}
			this.setCartQty(code, val, { skipRender: false, debouncePricing: false });
		});

		root.on("click", "[data-remove-item]", (e) => {
			const code = $(e.currentTarget).attr("data-remove-item");
			this.state.cart = this.state.cart.filter((c) => c.item_code !== code);
			this.renderCart();
			this.refreshCartPricing();
		});

		root.on("click", "#cima-btn-emit", () => {
			this.submitQuickInvoice();
		});
	}

	switchTab(tab) {
		this.state.activeTab = tab;
		const showFacturar = tab === "facturar";
		this.wrapper.find("#cima-view-facturar").toggle(showFacturar);
		this.wrapper.find("#cima-view-inventario").toggle(!showFacturar);
		this.wrapper.find("[data-action='tab-inventory']").toggleClass("active", !showFacturar);
	}

	loadBootstrap() {
		frappe.call({
			method: "cima_ve.api.seller_pos.get_seller_bootstrap",
			callback: (r) => {
				if (!r.message) return;
				this.state.bootstrap = r.message;
				this.syncAdminVisibility();
				this.state.priceList = r.message.default_price_list || "";
				this.state.warehouse = r.message.default_warehouse || "";
				this.state.salesChannel = r.message.default_sales_channel || "Detal";

				const plOptions = (r.message.price_lists || [])
					.map((pl) => `<option value="${frappe.utils.escape_html(pl.name)}">${frappe.utils.escape_html(pl.name)} (${pl.currency})</option>`)
					.join("");
				const whOptions =
					`<option value="">Todos los almacenes</option>` +
					(r.message.warehouses || [])
						.map((w) => `<option value="${frappe.utils.escape_html(w.name)}">${frappe.utils.escape_html(w.warehouse_name || w.name)}</option>`)
						.join("");
				const rules = (r.message.commission_rules && r.message.commission_rules.length)
					? r.message.commission_rules
					: [];
				let chOptions = "";
				if (rules.length) {
					const defChannel = (r.message.default_sales_channel || "Detal").toLowerCase();
					const defRule = rules.find((ru) => (ru.channel || "").toLowerCase() === defChannel) || rules[0];
					this.state.commissionOption = defRule.label;
					this.state.salesChannel = defRule.channel || defRule.label;
					chOptions = rules
						.map((ru) => `<option value="${frappe.utils.escape_html(ru.label)}">${frappe.utils.escape_html(ru.label)}</option>`)
						.join("");
				} else {
					const chList = (r.message.sales_channels && r.message.sales_channels.length)
						? r.message.sales_channels
						: ["Detal", "Mayor", "Meta Ads"];
					this.state.commissionOption = this.state.salesChannel;
					chOptions = chList
						.map((ch) => `<option value="${frappe.utils.escape_html(ch)}">${frappe.utils.escape_html(ch)}</option>`)
						.join("");
				}
				if (!/mercadolibre/i.test(chOptions)) {
					chOptions += `<option value="MercadoLibre">MercadoLibre (Sin comisión)</option>`;
				}

				this.wrapper.find("#cima-price-list, #cima-inv-price-list").html(plOptions).val(this.state.priceList);
				this.wrapper.find("#cima-warehouse, #cima-inv-warehouse").html(whOptions).val(this.state.warehouse);
				this.wrapper.find("#cima-sales-channel").html(chOptions).val(this.state.commissionOption || this.state.salesChannel);

				const sellerOptions = Array.isArray(r.message.seller_options) ? r.message.seller_options : [];
				const currentUser = r.message.current_user || frappe.session.user || "";
				const sellerFull = r.message.current_user_fullname || frappe.session.user_fullname || currentUser;
				this.state.selectedSellerUser = currentUser;
				this.state.selectedSalesPerson = r.message.default_sales_person || "";

				if (this.isAdmin() && sellerOptions.length) {
					const optsHtml = sellerOptions
						.map((s) => {
							const sUser = s.user || "";
							const sFull = s.fullname || sUser;
							const sSp = s.sales_person || "";
							const isCurrent = sUser.toLowerCase() === currentUser.toLowerCase();
							const spTag = sSp && sSp.toLowerCase() !== sFull.toLowerCase() ? ` · ${sSp}` : "";
							const suffix = isCurrent ? " (Caja / Actual)" : spTag;
							return `<option value="${frappe.utils.escape_html(sUser)}" data-sales-person="${frappe.utils.escape_html(sSp)}" data-fullname="${frappe.utils.escape_html(sFull)}">${frappe.utils.escape_html(sFull)}${frappe.utils.escape_html(suffix)}</option>`;
						})
						.join("");
					this.wrapper.find("#cima-seller-select").html(optsHtml).val(currentUser);
					this.wrapper.find("#cima-admin-seller-box").show();
					const selOpt = this.wrapper.find("#cima-seller-select option:selected");
					this.state.selectedSalesPerson = (selOpt.attr("data-sales-person") || r.message.default_sales_person || "").trim();
					this.wrapper.find("#cima-logged-seller-badge").html(
						`Vendedor asignado: <strong>${frappe.utils.escape_html(sellerFull)}</strong>`
					);
				} else {
					this.wrapper.find("#cima-admin-seller-box").hide();
					this.wrapper.find("#cima-logged-seller-badge").html(
						`Vendedor: <strong>${frappe.utils.escape_html(sellerFull)}</strong>`
					);
				}

				this.loadCustomers("");
				this.loadInventory("");
				this.updateSummaryFromCart();
			},
		});
	}

	getBsRate() {
		const fx = this.state.bootstrap?.exchange_rate || {};
		return Number(fx.adjusted_rate || fx.bcv_rate || 0);
	}

	formatBs(amount) {
		return `Bs. ${Number(amount || 0).toLocaleString("es-VE", {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		})}`;
	}

	loadCustomers(query) {
		frappe.call({
			method: "cima_ve.api.seller_pos.search_customers",
			args: { query: query || "", limit: 25 },
			callback: (r) => {
				const rows = r.message || [];
				const options =
					`<option value="">-- Seleccionar cliente --</option>` +
					rows
						.map((c) => {
							const rif = c.ve_rif_cedula || c.tax_id || "Sin RIF";
							const em = c.email_id || "";
							return `<option value="${frappe.utils.escape_html(c.name)}" data-email="${frappe.utils.escape_html(em)}">${frappe.utils.escape_html(c.customer_name)} (${frappe.utils.escape_html(rif)})</option>`;
						})
						.join("");
				const select = this.wrapper.find("#cima-customer-select");
				select.html(options);
				if (this.state.selectedCustomer) {
					select.val(this.state.selectedCustomer);
					const optEmail = select.find("option:selected").attr("data-email") || "";
					if (optEmail && !this.wrapper.find("#cima-customer-email").val()) {
						this.wrapper.find("#cima-customer-email").val(optEmail);
					}
				}
			},
		});
	}

	loadInventory(query) {
		frappe.call({
			method: "cima_ve.api.seller_pos.search_inventory_and_prices",
			args: {
				query: query || "",
				price_list: this.state.priceList,
				warehouse: this.state.warehouse,
				limit: 80,
			},
			callback: (r) => {
				this.state.inventoryItems = r.message || [];
				this.renderInvoiceProductList();
				this.renderInventoryTable();
			},
		});
	}

	stockBadge(qty) {
		const n = Number(qty || 0);
		if (n <= 0) return `<span class="cima-stock-badge cima-stock-empty">Sin stock (0)</span>`;
		if (n <= 5) return `<span class="cima-stock-badge cima-stock-low">Stock bajo (${n})</span>`;
		return `<span class="cima-stock-badge cima-stock-ok">Disponible: ${n}</span>`;
	}

	renderInvoiceProductList() {
		const container = this.wrapper.find("#cima-invoice-item-results");
		if (!this.state.inventoryItems.length) {
			container.html(`<div class="text-muted text-center p-4">No se encontraron productos.</div>`);
			return;
		}
		const html = this.state.inventoryItems
			.map((item) => {
				const price = Number(item.price || 0).toFixed(2);
				const codeEsc = frappe.utils.escape_html(item.item_code);
				return `
					<div class="cima-product-row">
						<div>
							<div><strong>${frappe.utils.escape_html(item.item_name)}</strong></div>
							<div class="text-muted small">
								${codeEsc} · ${this.stockBadge(item.stock_qty)}
							</div>
						</div>
						<div class="text-right">
							<div class="font-weight-bold mb-1">${frappe.utils.escape_html(item.currency || "USD")} ${price}</div>
							<div class="d-flex justify-content-end" style="gap: 6px;">
								<button type="button" class="btn btn-xs btn-default" data-add-custom-price="${codeEsc}" title="Agregar con precio personalizado">
									Precio
								</button>
								<button type="button" class="btn btn-xs btn-primary" data-add-item="${codeEsc}">
									+ Agregar
								</button>
							</div>
						</div>
					</div>
				`;
			})
			.join("");
		container.html(html);
	}

	renderInventoryTable() {
		const tbody = this.wrapper.find("#cima-inv-table-body");
		if (!this.state.inventoryItems.length) {
			tbody.html(`<tr><td colspan="6" class="text-center text-muted">Sin resultados para esa búsqueda.</td></tr>`);
			return;
		}
		const html = this.state.inventoryItems
			.map((item) => {
				const price = Number(item.price || 0).toFixed(2);
				return `
					<tr>
						<td><code>${frappe.utils.escape_html(item.item_code)}</code></td>
						<td><strong>${frappe.utils.escape_html(item.item_name)}</strong></td>
						<td>${frappe.utils.escape_html(item.item_group || "-")}</td>
						<td class="text-right">${this.stockBadge(item.stock_qty)}</td>
						<td class="text-right"><strong>${frappe.utils.escape_html(item.currency || "USD")} ${price}</strong></td>
						<td class="text-center">
							<button type="button" class="btn btn-xs btn-success" data-add-item="${frappe.utils.escape_html(item.item_code)}">
								Facturar
							</button>
						</td>
					</tr>
				`;
			})
			.join("");
		tbody.html(html);
	}

	addItemToCart(item) {
		const existing = this.state.cart.find((c) => c.item_code === item.item_code);
		if (existing) {
			existing.qty += 1;
		} else {
			this.state.cart.push({
				item_code: item.item_code,
				item_name: item.item_name,
				qty: 1,
				price_list_rate: Number(item.price || 0),
				discount_percentage: 0,
				discount_amount: 0,
				rate: Number(item.price || 0),
				is_custom_price: false,
				currency: item.currency || "USD",
			});
		}
		this.renderCart();
		this.refreshCartPricing();
	}

	updateCartQty(code, delta) {
		const row = this.state.cart.find((c) => c.item_code === code);
		if (!row) return;
		row.qty = Math.max(1, row.qty + delta);
		this.renderCart();
		this.refreshCartPricing();
	}

	setCartQty(code, qty, opts = {}) {
		const row = this.state.cart.find((c) => c.item_code === code);
		if (!row) return;
		row.qty = Math.max(0.01, qty);
		if (opts.skipRender) {
			this.updateRowSubtotalDom(row);
			this.updateSummaryFromCart();
		} else {
			this.renderCart();
		}
		if (opts.debouncePricing) {
			clearTimeout(this._pricingTimer);
			this._pricingTimer = setTimeout(() => {
				this.refreshCartPricing();
			}, 280);
		} else {
			clearTimeout(this._pricingTimer);
			this.refreshCartPricing();
		}
	}

	formatRowPriceHtml(row) {
		const rate = Number(row.rate || 0);
		const listRate = Number(row.price_list_rate || rate);
		const discPct = Number(row.discount_percentage || 0);
		const hasDiscount = discPct > 0 || (listRate > 0 && rate < listRate - 0.0001);
		if (hasDiscount) {
			const pctText = discPct > 0
				? `-${discPct.toFixed(discPct % 1 === 0 ? 0 : 1)}%`
				: `-$${(listRate - rate).toFixed(2)}`;
			return `<del class="text-muted">${listRate.toFixed(2)}</del> <strong>${rate.toFixed(2)}</strong> c/u <span class="cima-discount-badge">${pctText}</span>`;
		}
		return `${rate.toFixed(2)} c/u`;
	}

	updateRowSubtotalDom(row) {
		const codeEsc = CSS.escape ? CSS.escape(row.item_code) : row.item_code;
		const sub = (row.qty * row.rate).toFixed(2);
		this.wrapper.find(`[data-item-subtotal="${codeEsc}"]`).text(sub);
		this.wrapper.find(`[data-item-price="${codeEsc}"]`).html(this.formatRowPriceHtml(row));
	}

	refreshCartPricing() {
		if (!this.state.cart.length) return;
		const seq = ++this._pricingSeq;
		frappe.call({
			method: "cima_ve.api.seller_pos.preview_cart_pricing",
			args: {
				items: JSON.stringify(this.state.cart),
				customer: this.state.selectedCustomer || "",
				price_list: this.state.priceList || "",
				warehouse: this.state.warehouse || "",
				apply_igtf: 0,
			},
			callback: (r) => {
				if (seq !== this._pricingSeq || !r.message) return;
				const priced = r.message.items || [];
				for (const p of priced) {
					const local = this.state.cart.find((c) => c.item_code === p.item_code);
					if (local) {
						local.price_list_rate = Number(p.price_list_rate ?? p.rate ?? local.rate);
						if (!local.is_custom_price) {
							local.discount_percentage = Number(p.discount_percentage || 0);
							local.discount_amount = Number(p.discount_amount || 0);
							local.rate = Number(p.rate ?? local.rate);
						} else {
							const orig = Number(local.price_list_rate || 0);
							const cur = Number(local.rate || 0);
							if (orig > 0 && cur < orig - 0.0001) {
								local.discount_amount = Number((orig - cur).toFixed(4));
								local.discount_percentage = Number((((orig - cur) / orig) * 100).toFixed(2));
							} else {
								local.discount_amount = 0;
								local.discount_percentage = 0;
							}
						}
					}
				}
				const isEditingQty = $(document.activeElement).is("[data-qty-input]");
				if (isEditingQty) {
					for (const row of this.state.cart) {
						this.updateRowSubtotalDom(row);
					}
					this.updateSummaryFromCart();
				} else {
					this.renderCart();
				}
			},
		});
	}

	updateSummaryFromCart(summaryOverride = null) {
		if (summaryOverride) {
			this.renderSummary(Number(summaryOverride.taxable_base || 0));
			return;
		}
		const base = this.state.cart.reduce((acc, r) => acc + r.qty * r.rate, 0);
		this.renderSummary(base);
	}

	renderCart(summaryOverride = null) {
		const container = this.wrapper.find("#cima-cart-items");
		const emitBtn = this.wrapper.find("#cima-btn-emit");

		if (!this.state.cart.length) {
			container.html(`<div class="text-muted text-center p-4">Agrega productos desde la izquierda para comenzar.</div>`);
			this.renderSummary(0);
			emitBtn.prop("disabled", true);
			return;
		}

		const rowsHtml = this.state.cart
			.map((row) => {
				const sub = (row.qty * row.rate).toFixed(2);
				const codeEsc = frappe.utils.escape_html(row.item_code);
				return `
					<div class="cima-product-row">
						<div>
							<div><strong>${frappe.utils.escape_html(row.item_name)}</strong></div>
							<div class="text-muted small" data-item-price="${codeEsc}">${this.formatRowPriceHtml(row)}</div>
						</div>
						<div class="d-flex align-items-center" style="gap: 6px;">
							<button type="button" class="btn btn-xs btn-default cima-btn-edit-price" data-edit-price="${codeEsc}" title="Cambiar precio">Precio</button>
							<button type="button" class="btn btn-xs btn-default" data-item-code="${codeEsc}" data-qty-delta="-1">-</button>
							<input type="number" class="form-control input-sm cima-qty-input" data-qty-input="${codeEsc}" value="${row.qty}" min="1" step="1" />
							<button type="button" class="btn btn-xs btn-default" data-item-code="${codeEsc}" data-qty-delta="1">+</button>
							<span class="ml-2 font-weight-bold" data-item-subtotal="${codeEsc}">${sub}</span>
							<button type="button" class="btn btn-xs btn-danger" data-remove-item="${codeEsc}">×</button>
						</div>
					</div>
				`;
			})
			.join("");
		container.html(`<div class="cima-product-list">${rowsHtml}</div>`);

		this.updateSummaryFromCart(summaryOverride);

		emitBtn.prop("disabled", !this.state.selectedCustomer || !this.state.cart.length || !this.state.dueDate);
	}

	computeGlobalDiscounts(base) {
		const subtotal = Math.max(Number(base || 0), 0);
		const boot = this.state.bootstrap || {};
		const autoEnabled = boot.auto_discount_enabled !== false;
		const tiers = Array.isArray(boot.auto_discount_tiers) && boot.auto_discount_tiers.length
			? boot.auto_discount_tiers
			: [
				{ label: "Compra < $100", min_amount: 0, max_amount: 99.99, discount_percentage: 0 },
				{ label: "Compra $500 - $1,000", min_amount: 500, max_amount: 1000, discount_percentage: 4.76 },
				{ label: "Compra > $1,000", min_amount: 1000.01, max_amount: null, discount_percentage: 9.09 },
			];

		let autoPct = 0;
		let autoLabel = "";
		let autoAmount = 0;
		if (autoEnabled && subtotal > 0) {
			const matches = tiers.filter((t) => {
				const minAmt = Number(t.min_amount || 0);
				const maxAmt = t.max_amount === null || t.max_amount === undefined || t.max_amount === "" ? null : Number(t.max_amount);
				if (subtotal + 1e-9 < minAmt) return false;
				if (maxAmt !== null && subtotal - 1e-9 > maxAmt) return false;
				return true;
			});
			if (matches.length) {
				matches.sort((a, b) => Number(b.discount_percentage || 0) - Number(a.discount_percentage || 0));
				const best = matches[0];
				const pct = Number(best.discount_percentage || 0);
				if (pct > 0) {
					autoPct = Number(pct.toFixed(2));
					autoLabel = best.label || "";
					autoAmount = Number((subtotal * (autoPct / 100)).toFixed(2));
				}
			}
		}

		const afterAuto = Math.max(Number((subtotal - autoAmount).toFixed(2)), 0);
		let manualPct = 0;
		let manualAmount = 0;
		if (this.state.manualDiscountEnabled && afterAuto > 0) {
			manualPct = Math.min(Math.max(Number(this.state.manualDiscountPct || 0), 0), 100);
			manualPct = Number(manualPct.toFixed(2));
			if (manualPct > 0) {
				manualAmount = Number((afterAuto * (manualPct / 100)).toFixed(2));
			}
		}

		const totalDiscount = Math.min(subtotal, Number((autoAmount + manualAmount).toFixed(2)));
		const finalTotal = Math.max(Number((subtotal - totalDiscount).toFixed(2)), 0);
		return {
			subtotal,
			autoPct,
			autoLabel,
			autoAmount,
			manualPct,
			manualAmount,
			totalDiscount,
			finalTotal,
		};
	}

	renderSummary(base) {
		const d = this.computeGlobalDiscounts(base);
		const bsRate = this.getBsRate();
		const totalBs = bsRate > 0 ? Number((d.finalTotal * bsRate).toFixed(2)) : 0;
		const autoLine = d.autoAmount > 0
			? `<div class="cima-summary-line" style="color:#92400e;"><span>Descuento automático (${d.autoPct}% · ${frappe.utils.escape_html(d.autoLabel)}):</span><strong>-${d.autoAmount.toFixed(2)}</strong></div>`
			: "";
		const manualLine = d.manualAmount > 0
			? `<div class="cima-summary-line" style="color:#92400e;"><span>Descuento manual (${d.manualPct}%):</span><strong>-${d.manualAmount.toFixed(2)}</strong></div>`
			: "";
		const bsLine = bsRate > 0
			? `<div class="cima-summary-line" style="color:#1d4ed8;font-weight:700;margin-top:4px;"><span>TOTAL (Bs.):</span><strong>${this.formatBs(totalBs)}</strong></div>
			   <div class="small text-muted text-right">Tasa: Bs. ${bsRate.toFixed(2)} / USD</div>`
			: "";
		this.wrapper.find("#cima-tax-summary").html(`
			<div class="cima-summary-line"><span>Subtotal:</span><strong>${d.subtotal.toFixed(2)}</strong></div>
			${autoLine}
			${manualLine}
			<div class="cima-summary-total"><span>TOTAL:</span><span>${d.finalTotal.toFixed(2)}</span></div>
			${bsLine}
		`);
	}

	openCustomPriceDialog(code) {
		const row = this.state.cart.find((c) => c.item_code === code);
		if (!row) return;
		const origPrice = Number(row.price_list_rate || row.rate || 0);
		const curPrice = Number(row.rate || 0);
		const currency = row.currency || "USD";

		const d = new frappe.ui.Dialog({
			title: `Precio Personalizado — ${row.item_name || row.item_code}`,
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "info_html",
					options: `<div class="mb-2 text-muted small">Precio original de lista: <strong>${frappe.utils.escape_html(currency)} ${origPrice.toFixed(2)}</strong></div>`,
				},
				{
					label: `Nuevo precio unitario (${currency})`,
					fieldname: "custom_rate",
					fieldtype: "Float",
					reqd: 1,
					default: curPrice,
				},
				{
					fieldtype: "HTML",
					fieldname: "preview_html",
					options: `<div class="cima-custom-price-preview small"></div>`,
				},
			],
			primary_action_label: "Aplicar Precio",
			primary_action: (values) => {
				const newRate = Number(values.custom_rate);
				if (isNaN(newRate) || newRate < 0) {
					frappe.msgprint("Ingresa un precio válido mayor o igual a 0.");
					return;
				}
				row.rate = Number(newRate.toFixed(4));
				row.is_custom_price = true;
				if (origPrice > 0 && row.rate < origPrice - 0.0001) {
					row.discount_amount = Number((origPrice - row.rate).toFixed(4));
					row.discount_percentage = Number((((origPrice - row.rate) / origPrice) * 100).toFixed(2));
				} else {
					row.discount_amount = 0;
					row.discount_percentage = 0;
				}
				d.hide();
				this.renderCart();
				this.refreshCartPricing();
			},
			secondary_action_label: row.is_custom_price ? "Restaurar precio original" : undefined,
			secondary_action: () => {
				row.is_custom_price = false;
				row.rate = origPrice;
				row.discount_amount = 0;
				row.discount_percentage = 0;
				d.hide();
				this.renderCart();
				this.refreshCartPricing();
			},
		});

		const updatePreview = () => {
			const val = parseFloat(d.get_value("custom_rate"));
			const box = d.$wrapper.find(".cima-custom-price-preview");
			if (isNaN(val) || val < 0) {
				box.html(`<span class="text-danger">Ingresa un monto válido.</span>`);
				return;
			}
			if (origPrice > 0 && val < origPrice - 0.0001) {
				const diff = origPrice - val;
				const pct = (diff / origPrice) * 100;
				box.html(`<span class="cima-discount-badge">Descuento: -${currency} ${diff.toFixed(2)} (-${pct.toFixed(pct % 1 === 0 ? 0 : 1)}%)</span>`);
			} else if (origPrice > 0 && val > origPrice + 0.0001) {
				box.html(`<span class="text-muted">Precio personalizado (+${currency} ${(val - origPrice).toFixed(2)} sobre lista)</span>`);
			} else {
				box.html(`<span class="text-muted">Igual al precio original de lista.</span>`);
			}
		};

		d.show();
		updatePreview();
		const inputEl = d.$wrapper.find("input[data-fieldname='custom_rate']");
		inputEl.on("input change", updatePreview);
		setTimeout(() => inputEl.focus().select(), 80);
	}

	openNewCustomerDialog() {
		const d = new frappe.ui.Dialog({
			title: "Nuevo Cliente Rápido",
			fields: [
				{ label: "Nombre / Razón Social", fieldname: "customer_name", fieldtype: "Data", reqd: 1 },
				{ label: "Cédula o RIF", fieldname: "rif_cedula", fieldtype: "Data", reqd: 1, description: "Ej: V-12345678 o J-12345678-9" },
				{ label: "Correo Electrónico", fieldname: "email", fieldtype: "Data", reqd: 1, description: "Obligatorio para enviar la Nota de Entrega" },
				{
					label: "Tipo de Contribuyente",
					fieldname: "taxpayer_type",
					fieldtype: "Select",
					options: "No Contribuyente\nOrdinario\nEspecial\nFormal",
					default: "No Contribuyente",
				},
			],
			primary_action_label: "Guardar Cliente",
			primary_action: (values) => {
				frappe.call({
					method: "cima_ve.api.seller_pos.quick_create_customer",
					args: values,
					callback: (r) => {
						if (r.message) {
							this.state.selectedCustomer = r.message.name;
							this.wrapper.find("#cima-customer-email").val(r.message.email_id || values.email || "");
							this.loadCustomers("");
							this.renderCart();
							d.hide();
							frappe.show_alert({ message: "Cliente creado", indicator: "green" });
						}
					},
				});
			},
		});
		d.show();
	}

	submitQuickInvoice() {
		if (!this.state.selectedCustomer || !this.state.cart.length) {
			frappe.msgprint("Selecciona un cliente y al menos un producto.");
			return;
		}
		const customerEmail = (this.wrapper.find("#cima-customer-email").val() || "").trim();
		if (!customerEmail || !customerEmail.includes("@")) {
			frappe.msgprint("El correo electrónico del cliente es obligatorio para emitir la Nota de Entrega.");
			this.wrapper.find("#cima-customer-email").focus();
			return;
		}
		const dueDate = (this.state.dueDate || this.wrapper.find("#cima-due-date").val() || "").trim();
		if (!dueDate) {
			frappe.msgprint("Debes indicar la Fecha de Vencimiento de la Nota de Entrega.");
			this.wrapper.find("#cima-due-date").focus();
			return;
		}
		this.state.dueDate = dueDate;
		const btn = this.wrapper.find("#cima-btn-emit");
		btn.prop("disabled", true).text("Emitiendo Nota de Entrega...");

		const submitArgs = {
			customer: this.state.selectedCustomer,
			customer_email: customerEmail,
			items: JSON.stringify(this.state.cart),
			price_list: this.state.priceList,
			warehouse: this.state.warehouse,
			sales_channel: this.state.salesChannel,
			commission_option: this.state.commissionOption || this.state.salesChannel,
			fecha_vencimiento: dueDate,
			manual_discount_enabled: this.state.manualDiscountEnabled ? 1 : 0,
			manual_discount_percentage: this.state.manualDiscountEnabled ? Number(this.state.manualDiscountPct || 0) : 0,
		};
		if (this.isAdmin() && this.state.selectedSellerUser) {
			submitArgs.seller_user = this.state.selectedSellerUser;
			if (this.state.selectedSalesPerson) {
				submitArgs.sales_person = this.state.selectedSalesPerson;
			}
		}

		frappe.call({
			method: "cima_ve.api.delivery_notes.create_quick_delivery_note",
			args: submitArgs,
			callback: (r) => {
				btn.text("EMITIR NOTA DE ENTREGA");
				if (r.message) {
					this.state.lastInvoiceResult = r.message;
					this.state.cart = [];
					this.state.dueDate = "";
					this.wrapper.find("#cima-due-date").val("");
					this.state.manualDiscountEnabled = false;
					this.state.manualDiscountPct = 0;
					this.wrapper.find("#cima-enable-discount").prop("checked", false);
					this.wrapper.find("#cima-discount-pct").val("0");
					this.wrapper.find("#cima-manual-discount-box").hide();
					this.renderCart();
					this.renderResultBanner();
					this.loadInventory(this.wrapper.find("#cima-invoice-item-search").val() || "");
				}
			},
			error: () => {
				btn.prop("disabled", false).text("EMITIR NOTA DE ENTREGA");
			},
		});
	}

	renderResultBanner() {
		const box = this.wrapper.find("#cima-result-area");
		const res = this.state.lastInvoiceResult;
		if (!res) {
			box.empty();
			return;
		}
		const seller = res.seller || {};
		const sellerLabel = seller.fullname || seller.user || frappe.session.user_fullname || frappe.session.user || "";
		const docName = res.delivery_note || res.invoice_name || "";
		const pdfLink = docName
			? ` · <a href="/api/method/cima_ve.api.delivery_notes.download_delivery_note_pdf?delivery_note=${encodeURIComponent(docName)}" target="_blank" rel="noopener" style="font-weight:700;">Descargar PDF</a>`
			: "";
		const notesLink = this.isAdmin()
			? ` · <a href="/app/notas-entrega" style="font-weight:700;">Gestionar en Notas de Entrega</a>`
			: ` · <a href="/app/notas-entrega" style="font-weight:700;">Ver en Mis Notas de Entrega</a>`;
		const discItems = res.discounted_items || [];
		const gi = res.discount_info || {};
		const globalDiscParts = [];
		if (Number(gi.auto_discount_amount || 0) > 0) {
			globalDiscParts.push(`Auto ${Number(gi.auto_discount_pct).toFixed(2)}% (-$${Number(gi.auto_discount_amount).toFixed(2)})`);
		}
		if (Number(gi.manual_discount_amount || 0) > 0) {
			globalDiscParts.push(`Manual ${Number(gi.manual_discount_pct).toFixed(2)}% (-$${Number(gi.manual_discount_amount).toFixed(2)})`);
		}
		const globalDiscLine = globalDiscParts.length
			? `<div class="small mt-1" style="color:#92400e;"><strong>Descuento global en Nota de Entrega:</strong> ${frappe.utils.escape_html(globalDiscParts.join(" + "))}</div>`
			: "";
		const discountLine = discItems.length
			? `<div class="small mt-1" style="color:#92400e;"><strong>Descuentos por producto:</strong> ${discItems
					.map((d) => `${frappe.utils.escape_html(d.item_name || d.item_code)} ($${Number(d.price_list_rate).toFixed(2)} → $${Number(d.rate).toFixed(2)})`)
					.join(", ")}</div>`
			: "";
		const commInfo = res.commission || {};
		const commOptText = commInfo.option ? ` · <strong>Modalidad:</strong> ${frappe.utils.escape_html(commInfo.option)}` : "";
		const dueDateText = res.fecha_vencimiento ? ` · <strong>Vence:</strong> ${frappe.utils.escape_html(res.fecha_vencimiento)}` : "";
		const bsRate = this.getBsRate();
		const bannerBs = bsRate > 0 ? ` (${this.formatBs(Number(res.grand_total || 0) * bsRate)})` : "";
		box.html(`
			<div class="cima-result-banner">
				<div><strong>Nota de Entrega emitida:</strong> ${frappe.utils.escape_html(docName)} — Total: ${frappe.utils.escape_html(res.currency)} ${Number(res.grand_total).toFixed(2)}${bannerBs}</div>
				<div class="small mt-1">
					<strong>Vendedor:</strong> ${frappe.utils.escape_html(sellerLabel)}${commOptText}${dueDateText} ·
					<strong>Estado:</strong> ${frappe.utils.escape_html(res.ve_ne_estado || "Pendiente")}${notesLink}${pdfLink}
				</div>
				${globalDiscLine}
				${discountLine}
			</div>
		`);
	}
}
