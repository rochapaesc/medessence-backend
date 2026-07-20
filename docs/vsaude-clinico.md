# vSaúde — Contrato dos endpoints clínicos (capturado em 20/07/2026)

Capturado via `public-api.vsaude.com.br` com `VSAUDE-API-KEY` (clínica real),
somente leitura. Complementa o `vsaude-swagger.json` (o swagger público lista
28 rotas, mas a API aceita mais — todos os abaixo foram validados ao vivo).

## Prontuário — `POST MedicalRecordEntryService/Get`

Body: `{"patientId": "<guid>"}` (aceita `PatientId`/`id` também).

```jsonc
{
  "records": [            // grupos por data de atendimento, desc
    {
      "date": "2026-06-01T14:17:45Z",
      "items": [          // entradas por discriminator:
        // NoteMedicalRecord   → {id, date, text(HTML), documents[], draftId, creator{id,name,licenceNumber}}
        // PrescriptionMedicalRecord → {id, date, link(URL app.vsaude.com.br), creator}
        // ExamMedicalRecord   → {id, date, link(URL), creator}
        // FormResponseMedicalRecord → {id, date, title, description, formId,
        //     answers: [{label, answer, answerValue, fieldType, fieldId, required, creationTime}],
        //     horizontalEvolution, creator}
      ]
    }
  ],
  "forms": [],              // catálogos de formulário (vazio na amostra)
  "dates": [1748787465.03], // timestamps dos grupos
  "horizontalRecords": [],
  "legacyHorizontalRecords": [],
  "childGrowth": [],        // curvas de crescimento (pediatria)
  "pageSize": 10
}
```

- `fieldType` observados em FormResponse: `textbox`, `multiple-selection-list`.
- `link` de Prescription/Exam aponta para `https://app.vsaude.com.br/...`
  (visualização autenticada do documento — NÃO é a public-api; re-hospedar
  exige sessão do app ou `DocumentsService/Export`, a calibrar).
- `text` da nota é HTML (sanitizar na entrada, §6.2).

## Exames solicitados — `GET ExaminationService/GetExaminations?PatientId=<guid>`

Lista plana:
`{id(guid), description, examinationDescription, doctorId(guid), creationTime,
isDeleted, creator/creatorUserId, lastModification*, deleter*}`.

## Modelos de prescrição — `POST PrescriptionModelService/GetAll`

Paginado `{totalCount, items}`; item:
`{id, name, content(HTML), hint, medications, smart, specialPrescription,
allowDelete, creationTime, ...}`.

## Disponibilidade (form Nova consulta!)

- `POST ScheduleService/GetAvailability`
  `{professionalId, procedureId, careUnitId, date}` →
  `{date, proposedDateHasAvailability, times[]}` (horários livres do dia).
- `POST ScheduleService/GetAvailabilityWindow`
  `{professionalId, procedureId, careUnitId, startDate, endDate}` →
  `[{date, availability}]` (dias com vaga na janela).

Substituem a inferência via workJourney da unidade quando houver EHR.

## Arquivos do paciente — `POST FilesService/ListFolder`

Body ou query `{ownerPatient: <guid>}` → árvore
`{id, isDirectory, folders[], files[], allowDelete, allowMove, icon, ...}`.
Upload existe (`FilesService/Upload?parent=&ownerPatient=&fileName=`) — fase
de escrita.

## Relatório de atendimentos — `POST ReportService/GetAttendance`

Body `{startDate, endDate}` → `{totalCount, totalDuration, totalPrice, items}`;
item: `{id, date, startTime, endTime, duration, plannedDuration, price,
clinicPricePart, otherPricePart, splitPortion, patient, healthProfessional,
careUnit, procedure, insuranceCompany, insurancePlan, remotely, status}`.
Fonte ideal para Parceiros/relatórios financeiros.

## Outras rotas confirmadas no swagger público (não capturadas ainda)

`ScheduleService/ReSchedule`, `ScheduleService/Reject`,
`ScheduleService/Snapshot`, `InsurancePlanService/GetAll`,
`PatientService/Search` (POST ?keyword=).
