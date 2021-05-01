# Copyright (c) 2020, Logedosoft Business Solutions and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
from erpnextturkish.eirsaliye.api.utils import to_base64, get_hash_md5, render_template
from frappe.contacts.doctype.address.address import get_default_address
from frappe.utils import format_datetime
import requests
import uuid
from bs4 import BeautifulSoup
from frappe.desk.form.utils import add_comment
from erpnextturkish import console



def on_submit_validate(doc, method):
    field_list = ["set_warehouse", "driver_name", "vehicle_no", "transporter_name"]
    translation_dict = {'set_warehouse': 'Ürün Çıkış Deposu', 'driver_name': 'Sürücü Adı', 'vehicle_no': 'Araç Plakası', 'transporter_name': 'Taşıyıcı'}
    for field in field_list:
        if not doc.get(field):
            frappe.throw(_("'{0}' alanı boş geçilemez!").format(translation_dict[field])) #frappe.throw(_("Field: '{0}' can not be emtpy in DocType: {1} {2}").format(field, doc.doctype, doc.name))


@frappe.whitelist()
def send_eirsaliye(delivery_note_name):
    import urllib3

    strResult = ""

    urllib3.disable_warnings()#Remove TLS warning

    doc = frappe.get_doc("Delivery Note", delivery_note_name)

    if not doc.eirsaliye_uuid:
        doc.eirsaliye_uuid = str(uuid.uuid1())
        doc.db_update()
        frappe.db.commit()
        doc.reload()
    if doc.yerelbelgeoid:
        data = validate_eirsaliye(delivery_note_name)
        if data.get("belgeNo"):
            return data
    
    #Validate the DN fields.
    validate_delivery_note(doc)

    if doc.belgeno:#If belgeno field is not empty
        dctResponse = validate_eirsaliye(doc.name)
        if dctResponse.get('durum') != 2:#If durum is not xml error return it back so we should send the DN only if we hasn't sent it or if we had an XML error before.
            return

    eirsaliye_settings = frappe.get_all("E Irsaliye Ayarlar", filters = {"company": doc.company})[0]
    settings_doc = frappe.get_doc("E Irsaliye Ayarlar", eirsaliye_settings)
    validate_settings_doc(settings_doc)

    set_warehouse_address_doc = frappe.get_doc("Address", get_default_address("Warehouse", doc.set_warehouse))
    set_warehouse_address_doc = set_missing_address_values(set_warehouse_address_doc)
    validate_address(set_warehouse_address_doc)

    customer_doc = frappe.get_doc("Customer", doc.customer)
    validate_customer(customer_doc)
    customer_address_doc = frappe.get_doc("Address", doc.shipping_address_name)
    customer_address_doc = set_missing_address_values(customer_address_doc)
    validate_address(customer_address_doc)
    
    doc = set_driver_name(doc)
    doc.posting_time = format_datetime(str(doc.posting_time), "HH:mm:ss")
    
    user = {}
    user["full_name"] = frappe.get_value("User",frappe.session.user,"full_name")

    for item in doc.items:
        eirsaliye_birimi_list = frappe.get_list("TD EIrsaliye Birim Eslestirme",
            filters={
                "parent": settings_doc.name,
                "td_birim": item.uom,    
            },
            fields = ["td_eirsaliye_birimi"]
        )
        if len(eirsaliye_birimi_list) == 0:
            frappe.throw(_("Please set {0} uom in e-Receipt settings for company {1}.").format(item.uom, eirsaliye_settings))
        td_eirsaliye_birimi = eirsaliye_birimi_list[0]["td_eirsaliye_birimi"]
        item.td_eirsaliye_birimi = td_eirsaliye_birimi


    data_context = {
        "delivery_note_doc" : doc,
        "settings_doc": settings_doc,
        "set_warehouse_address_doc" : set_warehouse_address_doc,
        "customer_doc": customer_doc,
        "customer_address_doc": customer_address_doc,
        "user": user,
    }

    outputText = render_template(data_context, file=settings_doc.xml_data)  # this is where to put args to the template renderer
    settings_doc.veri = to_base64(outputText)
    settings_doc.belgeHash = get_hash_md5(outputText)
    
    endpoint = settings_doc.test_eirsaliye_url if settings_doc.test_modu else settings_doc.eirsaliye_url
    settings_doc.password_uncoded = settings_doc.get_password('password')
    
    body_context = {
        "delivery_note_doc" : doc,
        "settings_doc": settings_doc,
    }

    body = render_template(body_context, file=settings_doc.xml_body)
    body = body.encode('utf-8')
    session = requests.session()
    session.headers = {"Content-Type": "text/xml; charset=utf-8"}
    session.headers.update({"Content-Length": str(len(body))})
    response = session.post(url=endpoint, data=body, verify=False)
    xml = response.content
    #frappe.msgprint(xml)
    #print("XML CONTENT IS")
    #print(xml)
    add_comment(doc.doctype, doc.name, str(xml), doc.modified_by)
    soup = BeautifulSoup(xml, 'xml')
    error = soup.find_all('Fault')
    belgeOid = soup.find_all('belgeOid')

    if error:
        faultcode = soup.find('faultcode').getText()
        faultstring = soup.find('faultstring').getText()
        #frappe.msgprint(str(faultcode) + " " + str(faultstring))
        
        #strResult = str(faultcode) + " " + str(faultstring)
        dctResult = {'result': False, 'description': _("Error Code:{0}. Error Description:{1}").format(faultcode, faultstring)}
    
    if belgeOid:
        msg = soup.find('belgeOid').getText()
        doc.yerelbelgeoid = msg
        doc.db_update()
        frappe.db.commit()
        doc.reload()
        #frappe.msgprint(str(msg))

        #strResult = str(msg)
        dctResult = {'result': True, 'description': msg}

    return dctResult#strResult


def set_missing_address_values(address_doc):
    city_subdivision_name = ""
    city_name = ""
    if address_doc.city:
        if "/" in address_doc.city:
            city_split = address_doc.city.split('/')
            city_subdivision_name = city_split[1]
            city_name = city_split[0]
        else:
            city_name = address_doc.city
    
    address_doc.city_subdivision_name = city_subdivision_name
    address_doc.city_name = city_name

    return address_doc


def set_driver_name(doc):
    driver_first_name = ""
    driver_family_name = ""
    if doc.driver_name:
        driver_name_split = doc.driver_name.split(' ')
        if len(driver_name_split) == 1:
            driver_first_name = doc.driver_name
        elif len(driver_name_split) == 2:
            driver_first_name = driver_name_split[0]
            driver_family_name = driver_name_split[1]
        elif len(driver_name_split) == 3:
            driver_first_name = "{0} {1}".format(driver_name_split[0], driver_name_split[1]) 
            driver_family_name = driver_name_split[2]
        else:
            driver_first_name = "{0} {1}".format(driver_name_split[0], driver_name_split[1]) 
            driver_family_name = "{0} {1}".format(driver_name_split[2], driver_name_split[3])
        
    doc.driver_first_name = driver_first_name
    doc.driver_family_name = driver_family_name

    return doc
    

def validate_settings_doc(doc):
    field_list = ["vergi_no", "td_vergi_no", "td_adres_sokak", "td_adres_bina_no",
     "td_adres_ilce", "td_adres_il", "td_posta_kodu", "td_adres_ulke", "vergi_no",
     "td_firma_adi"]
    for field in field_list:
        if not doc.get(field):
            frappe.throw(_("Field: '{0}' can not be emtpy in DocType: {1} {2}").format(field, doc.doctype, doc.name))


def validate_delivery_note(doc):
    field_list = ["set_warehouse", "eirsaliye_uuid", "driver_name", "vehicle_no"]
    for field in field_list:
        if not doc.get(field):
            frappe.throw(_("Field: '{0}' can not be emtpy in DocType: {1} {2}").format(field, doc.doctype, doc.name))


def validate_address(doc):
    field_list = ["address_line1", "city", "pincode", "country", "phone",
     "fax", "email_id"]
    for field in field_list:
        if not doc.get(field):
            frappe.throw(_("Field: '{0}' can not be emtpy in DocType: {1} {2}").format(field, doc.doctype, doc.name))


def validate_customer(doc):
    field_list = ["tax_id", "ld_tax_office"]
    for field in field_list:
        if not doc.get(field):
            frappe.throw(_("Field: '{0}' can not be emtpy in DocType: {1} {2}").format(field, doc.doctype, doc.name))
    if doc.customer_type == "Company" and len(doc.tax_id) != 10:
        frappe.throw(_("Tax ID field must have 10 numeric characters for Companys: {0}").format(doc.customer_name))
    elif doc.customer_type == "Individual" and len(doc.tax_id) != 11:
        frappe.throw(_("Tax ID field must have 11 numeric characters for Individuals: {0}").format(doc.customer_name))


@frappe.whitelist()
def validate_eirsaliye(delivery_note_name):
    doc = frappe.get_doc("Delivery Note", delivery_note_name)
    if not doc.yerelbelgeoid and not doc.belgeno:
        frappe.throw(_("Please send the delivery note first!"))
    
    eirsaliye_settings = frappe.get_all("E Irsaliye Ayarlar", filters = {"company": doc.company})[0]
    settings_doc = frappe.get_doc("E Irsaliye Ayarlar", eirsaliye_settings)
    endpoint = settings_doc.test_eirsaliye_url if settings_doc.test_modu else settings_doc.eirsaliye_url
    user = settings_doc.user_name
    password = settings_doc.get_password('password')
    body_context = {
        "user": user,
        "password": password,
        "td_vergi_no": settings_doc.td_vergi_no,
        "belgeno": doc.yerelbelgeoid or doc.belgeno,
    }
    body = render_template(body_context, file_name="validate_eirsaliye.xml")
    body = body.encode('utf-8')
    session = requests.session()
    session.headers = {"Content-Type": "text/xml; charset=utf-8"}
    session.headers.update({"Content-Length": str(len(body))})
    response = session.post(url=endpoint, data=body, verify=False)
    xml = response.content
    soup = BeautifulSoup(xml, 'xml')
    msg = {}
    if soup.find_all('belgeOid'):
        msg["belgeOid"] = soup.find('belgeOid').getText()
    if soup.find_all('faultstring'):
        msg["faultstring"] = soup.find('faultstring').getText()
    if soup.find_all('faultcode'):
        msg["faultcode"] = soup.find('faultcode').getText()
    if soup.find_all('aciklama'):
        msg["aciklama"] = soup.find('aciklama').getText()
    if soup.find_all('alimTarihi'):
        msg["alimTarihi"] = soup.find('alimTarihi').getText()
    if soup.find_all('ettn'):
        msg["ettn"] = soup.find('ettn').getText()
    if soup.find_all('belgeNo'):
        msg["belgeNo"] = soup.find('belgeNo').getText()
    if soup.find_all('gonderimCevabiDetayi'):
        msg["gonderimCevabiDetayi"] = soup.find('gonderimCevabiDetayi').getText()
    if soup.find_all('olusturulmaTarihi'):
        msg["olusturulmaTarihi"] = soup.find('olusturulmaTarihi').getText()
    if soup.find_all('yanitDetayi'):
        msg["yanitDetayi"] = soup.find('yanitDetayi').getText()
    if soup.find_all('durum'):
        msg["durum"] = soup.find('durum').getText()
    if soup.find_all('gonderimCevabiKodu'):
        msg["gonderimCevabiKodu"] = soup.find('gonderimCevabiKodu').getText()
    if soup.find_all('gonderimDurumu'):
        msg["gonderimDurumu"] = soup.find('gonderimDurumu').getText()
    if soup.find_all('yanitDurumu'):
        msg["yanitDurumu"] = soup.find('yanitDurumu').getText()
    if soup.find_all('ulastiMi'):
        msg["ulastiMi"] = soup.find('ulastiMi').getText()
    if soup.find_all('yenidenGonderilebilirMi'):
        msg["yenidenGonderilebilirMi"] = soup.find('yenidenGonderilebilirMi').getText()
    if soup.find_all('yerelBelgeOid'):
        msg["yerelBelgeOid"] = soup.find('yerelBelgeOid').getText()

    if msg.get("belgeNo") and not doc.belgeno:
        doc.belgeno = msg.get("belgeNo")
    if msg.get("durum"):
        doc.durum = msg.get("durum")
    if msg.get("yenidenGonderilebilirMi"):
        doc.yenidengonderilebilirmi = msg.get("yenidenGonderilebilirMi")
    if msg.get("gonderimCevabiDetayi"):
        doc.gonderimcevabidetayi = msg.get("gonderimCevabiDetayi")
    if msg.get("gonderimCevabiKodu"):
        doc.gonderimCevabiKodu = msg.get("gonderimCevabiKodu")
    if msg.get("gonderimDurumu"):
        doc.gonderimdurumu = msg.get("gonderimDurumu")
    if msg.get("yerelBelgeOid") and not doc.yerelbelgeoid:
        doc.yerelbelgeoid = msg.get("yerelBelgeOid")
    doc.db_update()
    frappe.db.commit()
    
    add_comment(doc.doctype, doc.name, _("Durum Kontrol Cevabı: {0}".format(msg)), doc.modified_by)
    print(_("Durum Kontrol Cevabı: {0}".format(xml)))
    
    return {#return(msg)
        'result': True,
        'description': msg
    }


@frappe.whitelist()
def login_test(eirsaliye_settings):
    settings_doc = frappe.get_doc("E Irsaliye Ayarlar", eirsaliye_settings)
    endpoint = settings_doc.test_eirsaliye_url if settings_doc.test_modu else settings_doc.eirsaliye_url
    user_name = settings_doc.user_name
    password = settings_doc.get_password('password')
    body_context = {
        "user_name": user_name,
        "password": password,
    }
    body = render_template(body_context, file_name="login_test.xml")
    body = body.encode('utf-8')
    session = requests.session()
    session.headers = {"Content-Type": "text/xml; charset=utf-8"}
    session.headers.update({"Content-Length": str(len(body))})
    response = session.post(url=endpoint, data=body, verify=False)
    xml = response.content
    xml = xml.decode('utf-8')
    if response.status_code == 200:
        frappe.msgprint(_("You have logged in Successfully"))
    else:
        soup = BeautifulSoup(xml, 'xml')
        if soup.find_all('faultstring'):
            faultstring = soup.find('faultstring').getText()
            frappe.throw(_(faultstring))
    return(xml)
