// Shared tax category definitions used across the dashboard.
// Single source of truth — import from here instead of defining inline.

export interface CategoryOption {
	value: string;
	label: string;
}

export const BUSINESS_CATEGORIES: CategoryOption[] = [
	{ value: 'ADVERTISING',           label: 'Advertising' },
	{ value: 'CAR_AND_TRUCK',          label: 'Car & Truck' },
	{ value: 'CONTRACT_LABOR',         label: 'Contract Labor' },
	{ value: 'INSURANCE',              label: 'Insurance' },
	{ value: 'LEGAL_AND_PROFESSIONAL', label: 'Legal & Professional' },
	{ value: 'OFFICE_EXPENSE',         label: 'Office Expense' },
	{ value: 'SUPPLIES',               label: 'Supplies' },
	{ value: 'TAXES_AND_LICENSES',     label: 'Taxes & Licenses' },
	{ value: 'TRAVEL',                 label: 'Travel' },
	{ value: 'MEALS',                  label: 'Meals (50%)' },
	{ value: 'COGS',                   label: 'COGS' },
	{ value: 'CONSULTING_INCOME',      label: 'Consulting Income' },
	{ value: 'SUBSCRIPTION_INCOME',    label: 'Subscription Income' },
	{ value: 'SALES_INCOME',           label: 'Sales Income' },
	{ value: 'REIMBURSABLE',           label: 'Reimbursable' },
	{ value: 'CAPITAL_CONTRIBUTION',   label: 'Capital Contribution' },
	{ value: 'OTHER_EXPENSE',          label: 'Other Expense (L27a)' },
];

export const PERSONAL_CATEGORIES: CategoryOption[] = [
	{ value: 'CHARITABLE_CASH',         label: 'Charitable (Cash)' },
	{ value: 'CHARITABLE_STOCK',        label: 'Charitable (Stock)' },
	{ value: 'MEDICAL',                 label: 'Medical' },
	{ value: 'STATE_LOCAL_TAX',         label: 'State & Local Tax' },
	{ value: 'MORTGAGE_INTEREST',       label: 'Mortgage Interest' },
	{ value: 'INVESTMENT_INCOME',       label: 'Investment Income' },
	{ value: 'PERSONAL_NON_DEDUCTIBLE', label: 'Personal (Non-deductible)' },
];

// All categories combined — for flat dropdowns that don't need grouping.
export const ALL_CATEGORIES: CategoryOption[] = [
	...BUSINESS_CATEGORIES,
	...PERSONAL_CATEGORIES,
];

// Human-readable labels keyed by category value.
// Prefer explicit labels over auto-generated ones for accuracy (e.g. "Meals (50%)" vs "Meals").
export const CATEGORY_LABELS: Record<string, string> = Object.fromEntries(
	ALL_CATEGORIES.map(c => [c.value, c.label])
);

// Subcategories grouped by parent category for the TransactionCard detail editor.
export const SUBCATEGORIES: Record<string, CategoryOption[]> = {
	ADVERTISING: [
		{ value: 'social_ads',      label: 'Social Media Ads (Pinterest, FB)' },
		{ value: 'race_promotion',  label: 'Race / Event Promotion' },
		{ value: 'print_marketing', label: 'Print Marketing' },
	],
	CONTRACT_LABOR: [
		{ value: 'photography',  label: 'Photography' },
		{ value: 'brand_design', label: 'Brand / Design Work' },
	],
	SUPPLIES: [
		{ value: 'ai_services',         label: 'AI Services (APIs)' },
		{ value: 'software_tools',      label: 'Software / SaaS' },
		{ value: 'ecommerce_platform',  label: 'Ecommerce Platform Fees' },
		{ value: 'payment_processing',  label: 'Payment Processing Fees' },
		{ value: 'packaging',           label: 'Packaging & Labels' },
		{ value: 'shipping',            label: 'Shipping (Outbound)' },
		{ value: 'shipping_inbound',    label: 'Shipping (Inbound / Freight)' },
		{ value: 'hardware',            label: 'Hardware' },
		{ value: 'office_supplies',     label: 'Office Supplies' },
		{ value: 'software',            label: 'Software (General)' },
	],
	TRAVEL: [
		{ value: 'flights',          label: 'Flights' },
		{ value: 'lodging',          label: 'Lodging' },
		{ value: 'ground_transport', label: 'Ground Transport' },
		{ value: 'wifi',             label: 'Wi-Fi' },
	],
	MEALS: [
		{ value: 'meals_team',   label: 'Team / Rider Dinner' },
		{ value: 'meals_event',  label: 'Race / Event Meal' },
		{ value: 'meals_client', label: 'Client Meal' },
		{ value: 'meals_solo',   label: 'Solo Business Meal' },
	],
	COGS: [
		{ value: 'raw_materials',  label: 'Raw Materials (Sourcing)' },
		{ value: 'manufacturing',  label: 'Manufacturing / Production' },
		{ value: 'inventory',      label: 'Finished Goods / Inventory' },
		{ value: 'shipping_cogs',  label: 'Shipping (COGS)' },
	],
	CONSULTING_INCOME: [
		{ value: 'consulting', label: 'Consulting' },
	],
	SUBSCRIPTION_INCOME: [
		{ value: 'subscription', label: 'Subscription' },
	],
	SALES_INCOME: [
		{ value: 'product_sales', label: 'Product Sales' },
		{ value: 'event_income',  label: 'Event Income' },
	],
	CHARITABLE_CASH: [
		{ value: 'cash_donation', label: 'Cash Donation' },
	],
	CHARITABLE_STOCK: [
		{ value: 'stock_donation', label: 'Stock Donation' },
	],
	INVESTMENT_INCOME: [
		{ value: 'capital_gain_short', label: 'Short-Term Capital Gain' },
		{ value: 'capital_gain_long',  label: 'Long-Term Capital Gain' },
		{ value: 'dividend',           label: 'Dividend' },
	],
};
